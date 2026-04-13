import json
import logging
from core.classifier import classify_reply
from core.reply_generator import generate_reply, match_faq, parse_time_confirmation, generate_meeting_confirmation
from core.calendar_manager import get_free_slots, create_meeting_event, format_slots_for_reply
from core.instantly_client import send_reply
from core.slack_notifier import notify_reply_sent, notify_escalation, notify_unknown_campaign, notify_meeting_booked, notify_error
from core.self_improver import get_best_examples, get_anti_patterns
from core.client_loader import get_client_by_campaign
from db.database import (
    is_duplicate, reply_sent_within_cooldown, get_thread_reply_count,
    is_human_takeover, log_interaction, update_outcome,
    get_last_offered_slots, get_interactions_for_lead, log_meeting,
)
import config

if config.TEST_MODE:
    from core.sheets_logger import log_test_reply
from core.email_notifier import notify_escalation_email, notify_interested_email

logger = logging.getLogger(__name__)


async def handle_instantly_webhook(payload: dict, db, clients: dict, confidence_threshold: float) -> dict:
    # 1. Validate event type
    if payload.get("event_type") != "reply_received":
        return {"status": "ignored", "reason": "not a reply event"}

    email_id = payload.get("email_id", "")
    lead_email = payload.get("lead_email", "")
    campaign_id = payload.get("campaign_id", "")

    # 2. Deduplicate
    if await is_duplicate(db, email_id):
        return {"status": "ignored", "reason": "duplicate"}

    # 3. Find client config
    client_config = get_client_by_campaign(clients, campaign_id)
    if not client_config:
        logger.warning(f"Unknown campaign {campaign_id} for lead {lead_email}")
        if config.TEST_MODE:
            log_test_reply(
                campaign_name=f"unknown:{campaign_id[:8]}",
                client_id="UNKNOWN",
                lead_email=lead_email,
                company="",
                original_message=payload.get("reply_text", "")[:500],
                classification="UNKNOWN_CAMPAIGN",
                confidence=0,
                generated_reply="(kampanija nesukonfigūruota)",
                sending_account=payload.get("email_account", ""),
                status="skipped_unknown_campaign",
            )
        await notify_unknown_campaign(campaign_id, lead_email)
        return {"status": "error", "reason": "unknown campaign"}

    client_id = client_config["client_id"]

    # 4. Human takeover check
    if await is_human_takeover(db, lead_email, campaign_id):
        return {"status": "ignored", "reason": "human took over"}

    # 5. Cooldown check
    if await reply_sent_within_cooldown(db, lead_email, campaign_id, config.REPLY_COOLDOWN_HOURS):
        return {"status": "ignored", "reason": "cooldown active"}

    # 6. Thread limit
    thread_count = await get_thread_reply_count(db, lead_email, campaign_id)
    if thread_count >= config.MAX_REPLIES_PER_THREAD:
        await notify_escalation(lead_email, payload.get("campaign_name", ""), "max replies reached", payload.get("reply_text", ""))
        return {"status": "escalated", "reason": "max replies reached"}

    thread_position = thread_count + 1
    reply_text = payload.get("reply_text", "")

    # 7. Update previous interaction outcome to replied_again
    prev_interactions = await get_interactions_for_lead(db, lead_email, campaign_id)
    if prev_interactions:
        last = prev_interactions[-1]
        if last["outcome"] is None and last["was_sent"]:
            await update_outcome(db, last["id"], "replied_again")

    # 8. Classify
    campaign_context = client_config.get("client_name", "") or payload.get("campaign_name", "")
    classification = await classify_reply(reply_text, campaign_context, thread_position)

    # 9. Below confidence threshold → UNCERTAIN
    if classification.confidence < confidence_threshold and classification.category not in ("UNSUBSCRIBE", "OUT_OF_OFFICE"):
        classification.category = "UNCERTAIN"

    # 10. Route by category
    if classification.category == "UNCERTAIN":
        if config.TEST_MODE:
            log_test_reply(
                campaign_name=payload.get("campaign_name", ""), client_id=client_id,
                lead_email=lead_email, company=payload.get("company_name", ""),
                original_message=reply_text, classification="UNCERTAIN",
                confidence=classification.confidence, generated_reply=f"(eskaluota: {classification.reasoning[:100]})",
                sending_account=payload.get("email_account", ""), status="escalated",
            )
        notify_escalation_email(lead_email, client_id, classification.category,
                               classification.confidence, reply_text, classification.reasoning)
        await notify_escalation(lead_email, payload.get("campaign_name", ""), f"uncertain ({classification.confidence:.0%})", reply_text)
        await log_interaction(db, {
            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
            "lead_email": lead_email, "email_account": payload.get("email_account"),
            "email_id": email_id, "client_id": client_id,
            "prospect_message": reply_text, "classification": classification.category,
            "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
            "was_sent": False, "thread_position": thread_position,
        })
        return {"status": "escalated", "reason": "uncertain"}

    if classification.category == "UNSUBSCRIBE":
        if config.TEST_MODE:
            log_test_reply(
                campaign_name=payload.get("campaign_name", ""), client_id=client_id,
                lead_email=lead_email, company=payload.get("company_name", ""),
                original_message=reply_text, classification="UNSUBSCRIBE",
                confidence=classification.confidence, generated_reply="(unsubscribe — pašalintas)",
                sending_account=payload.get("email_account", ""), status="unsubscribed",
            )
        for prev in prev_interactions:
            if prev["outcome"] is None:
                await update_outcome(db, prev["id"], "unsubscribed")
        await log_interaction(db, {
            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
            "lead_email": lead_email, "email_account": payload.get("email_account"),
            "email_id": email_id, "client_id": client_id,
            "prospect_message": reply_text, "classification": "UNSUBSCRIBE",
            "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
            "was_sent": False, "thread_position": thread_position,
        })
        return {"status": "logged", "reason": "unsubscribe"}

    if classification.category == "OUT_OF_OFFICE":
        if config.TEST_MODE:
            log_test_reply(
                campaign_name=payload.get("campaign_name", ""), client_id=client_id,
                lead_email=lead_email, company=payload.get("company_name", ""),
                original_message=reply_text, classification="OUT_OF_OFFICE",
                confidence=classification.confidence, generated_reply="(auto-reply, neatsakoma)",
                sending_account=payload.get("email_account", ""), status="out_of_office",
            )
        await log_interaction(db, {
            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
            "lead_email": lead_email, "email_account": payload.get("email_account"),
            "email_id": email_id, "client_id": client_id,
            "prospect_message": reply_text, "classification": "OUT_OF_OFFICE",
            "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
            "was_sent": False, "thread_position": thread_position,
        })
        return {"status": "logged", "reason": "out_of_office"}

    # 11. Categories that get a reply: INTERESTED, QUESTION, NOT_NOW, REFERRAL
    few_shots = await get_best_examples(db, classification.category, client_id, limit=3)
    anti_patterns = await get_anti_patterns(db, classification.category, client_id, limit=2)

    available_slots = None
    offered_slots_json = None
    matching_faq = None
    matched_faq_index = None
    faq_confidence = None

    if classification.category == "INTERESTED":
        prev_slots_json = await get_last_offered_slots(db, lead_email, campaign_id)
        if prev_slots_json:
            parsed = await parse_time_confirmation(reply_text, prev_slots_json)
            if parsed.get("confidence", 0) > 0.8 and parsed.get("confirmed_slot_index") is not None:
                prev_slots = json.loads(prev_slots_json)
                idx = parsed["confirmed_slot_index"]
                if 0 <= idx < len(prev_slots):
                    confirmed_slot = prev_slots[idx]
                    meeting = client_config["meeting"]
                    if config.TEST_MODE:
                        conf_reply = await generate_meeting_confirmation(
                            f"{confirmed_slot['day_name']} {confirmed_slot['time']}",
                            "https://meet.google.com/test-mode-link", meeting["duration_minutes"], client_config,
                        )
                        log_test_reply(
                            campaign_name=payload.get("campaign_name", ""),
                            client_id=client_id,
                            lead_email=lead_email,
                            company=payload.get("company_name", ""),
                            original_message=reply_text,
                            classification="INTERESTED",
                            confidence=classification.confidence,
                            generated_reply=conf_reply,
                            sending_account=payload.get("email_account", ""),
                            status="test_mode_meeting_would_book",
                        )
                        iid = await log_interaction(db, {
                            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
                            "lead_email": lead_email, "email_account": payload.get("email_account"),
                            "email_id": email_id, "client_id": client_id,
                            "prospect_message": reply_text, "classification": "INTERESTED",
                            "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
                            "agent_reply": conf_reply, "was_sent": False, "thread_position": thread_position,
                        })
                        await update_outcome(db, iid, "meeting_booked")
                        return {"status": "test_mode_meeting_would_book", "interaction_id": iid}
                    else:
                        event = await create_meeting_event(
                            calendar_id=meeting["google_calendar_id"],
                            prospect_email=lead_email,
                            start_iso=confirmed_slot["iso"],
                            duration_minutes=meeting["duration_minutes"],
                            meeting_purpose=meeting["purpose"],
                            client_participant=meeting["participant_from_client"],
                        )
                    if event:
                        conf_reply = await generate_meeting_confirmation(
                            f"{confirmed_slot['day_name']} {confirmed_slot['time']}",
                            event["meet_link"], meeting["duration_minutes"], client_config,
                        )
                        await send_reply(payload.get("email_account", ""), email_id, payload.get("reply_subject", ""), conf_reply)
                        iid = await log_interaction(db, {
                            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
                            "lead_email": lead_email, "email_account": payload.get("email_account"),
                            "email_id": email_id, "client_id": client_id,
                            "prospect_message": reply_text, "classification": "INTERESTED",
                            "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
                            "agent_reply": conf_reply, "was_sent": True, "thread_position": thread_position,
                        })
                        await update_outcome(db, iid, "meeting_booked")
                        await log_meeting(db, {
                            "interaction_id": iid, "lead_email": lead_email, "client_id": client_id,
                            "calendar_event_id": event["event_id"], "meeting_time": confirmed_slot["iso"],
                            "duration_minutes": meeting["duration_minutes"], "google_meet_link": event["meet_link"],
                        })
                        await notify_meeting_booked(lead_email, payload.get("campaign_name", ""), f"{confirmed_slot['day_name']} {confirmed_slot['time']}")
                        return {"status": "meeting_booked", "interaction_id": iid}
            await notify_escalation(lead_email, payload.get("campaign_name", ""), "galbūt patvirtino laiką, bet nesu tikras", reply_text)
            await log_interaction(db, {
                "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
                "lead_email": lead_email, "email_account": payload.get("email_account"),
                "email_id": email_id, "client_id": client_id,
                "prospect_message": reply_text, "classification": "UNCERTAIN",
                "confidence": classification.confidence, "classification_reasoning": "Time confirmation parse failed",
                "was_sent": False, "thread_position": thread_position,
            })
            return {"status": "escalated", "reason": "time confirmation unclear"}

        if not config.TEST_MODE:
            meeting = client_config["meeting"]
            try:
                available_slots = await get_free_slots(
                    calendar_id=meeting["google_calendar_id"],
                    working_hours=meeting["working_hours"],
                    duration=meeting["duration_minutes"],
                    advance_days=meeting["advance_days"],
                    num_slots=meeting["slots_to_offer"],
                    buffer_minutes=meeting.get("buffer_minutes", 15),
                )
                offered_slots_json = json.dumps(available_slots, ensure_ascii=False) if available_slots else None
            except Exception as e:
                logger.warning(f"Calendar slots unavailable: {e}")
                available_slots = None
        else:
            logger.info(f"TEST_MODE: skipping calendar slots for INTERESTED lead {lead_email}")

    if classification.category == "QUESTION":
        faq_result = await match_faq(reply_text, client_config.get("faq", []))
        matching_faq = faq_result.get("adapted_answer", "")
        matched_faq_index = faq_result.get("faq_index")
        faq_confidence = faq_result.get("confidence", 0)

        # If FAQ confidence is low — don't auto-reply, ask the human
        if faq_confidence is not None and faq_confidence < 0.7:
            from core.email_notifier import notify_unknown_question_email
            if config.TEST_MODE:
                log_test_reply(
                    campaign_name=payload.get("campaign_name", ""), client_id=client_id,
                    lead_email=lead_email, company=payload.get("company_name", ""),
                    original_message=reply_text, classification="QUESTION_UNKNOWN",
                    confidence=classification.confidence,
                    generated_reply="(laukiama tavo atsakymo)",
                    sending_account=payload.get("email_account", ""),
                    status="waiting_for_human",
                )
            iid = await log_interaction(db, {
                "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
                "lead_email": lead_email, "email_account": payload.get("email_account"),
                "email_id": email_id, "client_id": client_id,
                "prospect_message": reply_text, "classification": "QUESTION",
                "confidence": classification.confidence, "classification_reasoning": f"FAQ no match (confidence={faq_confidence})",
                "was_sent": False, "thread_position": thread_position,
            })
            notify_unknown_question_email(lead_email, client_id, reply_text, iid)
            logger.info(f"QUESTION with low FAQ confidence ({faq_confidence}) for {lead_email} — waiting for human")
            return {"status": "waiting_for_human", "interaction_id": iid, "reason": "FAQ no match"}

    # 12. Generate reply
    agent_reply = await generate_reply(
        prospect_message=reply_text,
        classification=classification.category,
        client_config=client_config,
        few_shots=few_shots,
        anti_patterns=anti_patterns,
        available_slots=available_slots,
        matching_faq=matching_faq,
    )

    # 13. Send via Instantly (or log to Sheets in TEST_MODE)
    if config.TEST_MODE:
        log_test_reply(
            campaign_name=payload.get("campaign_name", ""),
            client_id=client_id,
            lead_email=lead_email,
            company=payload.get("company_name", ""),
            original_message=reply_text,
            classification=classification.category,
            confidence=classification.confidence,
            generated_reply=agent_reply,
            sending_account=payload.get("email_account", ""),
            status="test_mode",
        )
        if classification.category == "INTERESTED":
            notify_interested_email(lead_email, client_id, reply_text, agent_reply)
        was_sent = False
    else:
        try:
            await send_reply(payload.get("email_account", ""), email_id, payload.get("reply_subject", ""), agent_reply)
        except Exception as e:
            await notify_error("instantly_send_failed", str(e))
            await log_interaction(db, {
                "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
                "lead_email": lead_email, "email_account": payload.get("email_account"),
                "email_id": email_id, "client_id": client_id,
                "prospect_message": reply_text, "classification": classification.category,
                "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
                "agent_reply": agent_reply, "was_sent": False, "thread_position": thread_position,
            })
            return {"status": "error", "reason": f"send failed: {e}"}
        was_sent = True

    # 14. Log interaction
    iid = await log_interaction(db, {
        "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
        "lead_email": lead_email, "email_account": payload.get("email_account"),
        "email_id": email_id, "client_id": client_id,
        "prospect_message": reply_text, "classification": classification.category,
        "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
        "agent_reply": agent_reply, "was_sent": was_sent,
        "matched_faq_index": matched_faq_index, "faq_confidence": faq_confidence,
        "offered_slots": offered_slots_json,
        "few_shots_used": json.dumps([fs["id"] for fs in few_shots]) if few_shots else None,
        "thread_position": thread_position,
    })

    # 15. Slack notification
    await notify_reply_sent(lead_email, payload.get("campaign_name", ""), classification.category, classification.confidence, reply_text, agent_reply)

    return {"status": "test_mode_logged" if config.TEST_MODE else "sent", "interaction_id": iid}
