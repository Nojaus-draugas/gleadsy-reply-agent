import asyncio
import json
import logging
import re
from core.classifier import classify_reply, APIUnavailableError, reset_usage_context
from core.reply_generator import generate_reply, match_faq, parse_time_confirmation, generate_meeting_confirmation
from core.calendar_manager import get_free_slots, create_meeting_event, format_slots_for_reply
from core.instantly_client import send_reply, add_to_blocklist, delete_lead_by_email
from core.slack_notifier import notify_reply_sent, notify_escalation, notify_unknown_campaign, notify_meeting_booked, notify_error, notify_approval_pending
from core.self_improver import get_best_examples, get_anti_patterns
from core.quality_reviewer import review_quality
from core.hallucination_guard import check_reply as check_hallucinations
from core.attachments import detect_attachments, detect_language_from_text
from core.client_loader import get_client_by_campaign, get_campaign_language
from core.translation import translate_to_lt
from core.language_detection import detect_language
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

# In-flight processing guard. If Instantly resends a webhook while we're still
# processing the first one (classify + send_reply takes 5-10s and Instantly may
# retry on slow responses), the in-DB dedup check would still pass because the
# row isn't logged until the end. This in-memory set blocks concurrent
# duplicates within the same process.
_in_flight: set[str] = set()


def _split_reply_and_history(reply_text: str) -> tuple[str, str]:
    """Išskiria naują prospect'o žinutę ir thread istoriją iš Instantly payload'o.

    Instantly siunčia reply_text su visu quote chain'u:
      "Naujoji prospect žinutė\n\nOn DATE Paulius rašė:\n> originalus cold email..."

    Grąžina (current_reply_only, thread_history_context).
    Thread history įtraukia viską nuo pirmo quote marker'io.
    """
    if not reply_text:
        return "", ""

    lines = reply_text.split("\n")
    split_idx = len(lines)

    # Quote markers (LT, EN, FR)
    patterns = [
        re.compile(r"^\s*>"),  # > quoted
        re.compile(r"^On\s+\w{3},?\s+\w+\s+\d+", re.I),  # "On Wed, Apr 22..."
        re.compile(r"^\d{4}-\d{2}-\d{2}.+(rašė|wrote|écrit)\s*:", re.I),
        re.compile(r"^Le\s+\d+\s+\w+", re.I),  # "Le 22 avril..."
        re.compile(r"^(From|Nuo|De):\s", re.I),
        re.compile(r"^-+\s*Original\s+[Mm]essage", re.I),
        re.compile(r"^-+\s*Pirminis", re.I),
        re.compile(r"^Sent from (my|an)\b", re.I),
        re.compile(r"^Išsiųsta iš", re.I),
        re.compile(r"^_{5,}"),  # ____________________
    ]

    for i, line in enumerate(lines):
        for p in patterns:
            if p.search(line):
                split_idx = i
                break
        if split_idx < len(lines):
            break

    current = "\n".join(lines[:split_idx]).strip()
    history_raw = "\n".join(lines[split_idx:]).strip()

    if not history_raw:
        return current, ""

    # Suformuoti history kaip readable context (nereikia > simbolių)
    hist_clean = re.sub(r"^\s*>\s?", "", history_raw, flags=re.MULTILINE)
    hist_clean = re.sub(r"\n{3,}", "\n\n", hist_clean).strip()

    return current, hist_clean


async def handle_instantly_webhook(payload: dict, db, clients: dict, confidence_threshold: float) -> dict:
    # 1. Validate event type
    if payload.get("event_type") != "reply_received":
        return {"status": "ignored", "reason": "not a reply event"}

    email_id = payload.get("email_id", "")
    lead_email = payload.get("lead_email", "")
    campaign_id = payload.get("campaign_id", "")

    # 2a. In-flight guard - block concurrent webhook retries for same email
    if email_id and email_id in _in_flight:
        logger.warning(f"Webhook duplicate (in-flight) for email_id={email_id}; ignoring")
        return {"status": "ignored", "reason": "in_flight"}

    # 2b. DB-level dedup (catches retries that arrive after we finished)
    if await is_duplicate(db, email_id):
        return {"status": "ignored", "reason": "duplicate"}

    if email_id:
        _in_flight.add(email_id)
    try:
        return await _process_reply(payload, db, clients, confidence_threshold,
                                     email_id, lead_email, campaign_id)
    finally:
        _in_flight.discard(email_id)


async def _process_reply(payload: dict, db, clients: dict, confidence_threshold: float,
                          email_id: str, lead_email: str, campaign_id: str) -> dict:
    # Reset per-request Claude usage akumuliatorių (cost tracking)
    reset_usage_context()

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
    try:
        classification = await classify_reply(reply_text, campaign_context, thread_position)
    except APIUnavailableError as e:
        logger.error(f"Claude API unavailable during classification for {lead_email}: {e}")
        await notify_error("claude_api_unavailable", f"Classification failed for {lead_email}: {e}")
        await notify_escalation(lead_email, payload.get("campaign_name", ""), "Claude API nepasiekiamas - klasifikacija nepavyko", reply_text)
        await log_interaction(db, {
            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
            "lead_email": lead_email, "email_account": payload.get("email_account"),
            "email_id": email_id, "client_id": client_id,
            "prospect_message": reply_text, "classification": "API_ERROR",
            "confidence": 0.0, "classification_reasoning": f"Claude API unavailable: {e}",
            "was_sent": False, "thread_position": thread_position,
            "reply_subject": payload.get("reply_subject", ""),
        })
        return {"status": "error", "reason": f"Claude API unavailable: {e}"}

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
            "reply_subject": payload.get("reply_subject", ""),
        })
        return {"status": "escalated", "reason": "uncertain"}

    if classification.category == "UNSUBSCRIBE":
        if config.TEST_MODE:
            log_test_reply(
                campaign_name=payload.get("campaign_name", ""), client_id=client_id,
                lead_email=lead_email, company=payload.get("company_name", ""),
                original_message=reply_text, classification="UNSUBSCRIBE",
                confidence=classification.confidence, generated_reply="(unsubscribe - pasalintas)",
                sending_account=payload.get("email_account", ""), status="unsubscribed",
            )
        for prev in prev_interactions:
            if prev["outcome"] is None:
                await update_outcome(db, prev["id"], "unsubscribed")

        # AUTO DELETE + BLOCKLIST (safeguard: confidence threshold + ENV toggle)
        auto_action_taken = False
        auto_action_note = ""
        if config.AUTO_BLOCKLIST_UNSUBSCRIBE and classification.confidence >= config.UNSUBSCRIBE_CONFIDENCE_MIN:
            try:
                bl_result = await add_to_blocklist(lead_email)
                del_result = await delete_lead_by_email(lead_email, campaign_id)
                auto_action_taken = True
                auto_action_note = (
                    f"blocklist={'ok' if bl_result.get('ok') else 'fail'}, "
                    f"deleted={del_result.get('deleted', 0)} lead(s)"
                )
                logger.info(f"UNSUBSCRIBE auto-action for {lead_email}: {auto_action_note}")
                try:
                    await notify_escalation(
                        lead_email, payload.get("campaign_name", ""),
                        f"UNSUBSCRIBE auto-action: {auto_action_note}",
                        reply_text,
                    )
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"UNSUBSCRIBE auto-action failed for {lead_email}: {e}")
                auto_action_note = f"error: {e}"

        await log_interaction(db, {
            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
            "lead_email": lead_email, "email_account": payload.get("email_account"),
            "email_id": email_id, "client_id": client_id,
            "prospect_message": reply_text, "classification": "UNSUBSCRIBE",
            "confidence": classification.confidence,
            "classification_reasoning": (classification.reasoning or "") + (f" | auto-action: {auto_action_note}" if auto_action_note else ""),
            "was_sent": False, "thread_position": thread_position,
            "reply_subject": payload.get("reply_subject", ""),
        })
        return {
            "status": "logged",
            "reason": "unsubscribe",
            "auto_action": auto_action_taken,
            "auto_action_note": auto_action_note,
        }

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
            "reply_subject": payload.get("reply_subject", ""),
        })
        return {"status": "logged", "reason": "out_of_office"}

    # 11. Categories that get a reply: INTERESTED, QUESTION, NOT_NOW, REFERRAL
    # Detect language and resolve approval setting
    campaign_lang_hint = get_campaign_language({client_id: client_config}, campaign_id) \
                         or client_config.get("tone", {}).get("language", "lt")
    original_language = detect_language(reply_text, campaign_lang_hint)
    approval_required = bool(client_config.get("approval_required", False))

    few_shots = await get_best_examples(db, classification.category, client_id,
                                         limit=3, language=original_language)
    anti_patterns = await get_anti_patterns(db, classification.category, client_id, limit=2)

    available_slots = None
    offered_slots_json = None
    matching_faq = None
    matched_faq_index = None
    faq_confidence = None

    if classification.category == "INTERESTED" and not approval_required:
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
                        try:
                            conf_reply = await generate_meeting_confirmation(
                                f"{confirmed_slot['day_name']} {confirmed_slot['time']}",
                                "https://meet.google.com/test-mode-link", meeting["duration_minutes"], client_config,
                            )
                        except APIUnavailableError as e:
                            logger.error(f"Claude API unavailable for meeting confirmation: {e}")
                            await notify_error("claude_api_unavailable", f"Meeting confirmation failed for {lead_email}: {e}")
                            await notify_escalation(lead_email, payload.get("campaign_name", ""), "Claude API nepasiekiamas - susitikimo patvirtinimas nepavyko", reply_text)
                            await log_interaction(db, {
                                "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
                                "lead_email": lead_email, "email_account": payload.get("email_account"),
                                "email_id": email_id, "client_id": client_id,
                                "prospect_message": reply_text, "classification": "INTERESTED",
                                "confidence": classification.confidence, "classification_reasoning": "Meeting confirmation generation failed",
                                "was_sent": False, "thread_position": thread_position,
                                "reply_subject": payload.get("reply_subject", ""),
                            })
                            return {"status": "error", "reason": f"Claude API unavailable: {e}"}
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
                            "reply_subject": payload.get("reply_subject", ""),
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
                        try:
                            conf_reply = await generate_meeting_confirmation(
                                f"{confirmed_slot['day_name']} {confirmed_slot['time']}",
                                event["meet_link"], meeting["duration_minutes"], client_config,
                            )
                        except APIUnavailableError as e:
                            logger.error(f"Claude API unavailable for meeting confirmation (live): {e}")
                            await notify_error("claude_api_unavailable", f"Meeting confirmation failed for {lead_email}: {e}")
                            await notify_escalation(lead_email, payload.get("campaign_name", ""),
                                                   f"Susitikimas sukurtas, bet patvirtinimo email nepavyko sugeneruoti. Meet link: {event['meet_link']}", reply_text)
                            await log_interaction(db, {
                                "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
                                "lead_email": lead_email, "email_account": payload.get("email_account"),
                                "email_id": email_id, "client_id": client_id,
                                "prospect_message": reply_text, "classification": "INTERESTED",
                                "confidence": classification.confidence, "classification_reasoning": "Meeting created but confirmation email generation failed",
                                "was_sent": False, "thread_position": thread_position,
                                "reply_subject": payload.get("reply_subject", ""),
                            })
                            return {"status": "error", "reason": f"Meeting created but confirmation generation failed: {e}"}
                        await send_reply(payload.get("email_account", ""), email_id, payload.get("reply_subject", ""), conf_reply)
                        iid = await log_interaction(db, {
                            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
                            "lead_email": lead_email, "email_account": payload.get("email_account"),
                            "email_id": email_id, "client_id": client_id,
                            "prospect_message": reply_text, "classification": "INTERESTED",
                            "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
                            "agent_reply": conf_reply, "was_sent": True, "thread_position": thread_position,
                            "reply_subject": payload.get("reply_subject", ""),
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
                "reply_subject": payload.get("reply_subject", ""),
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

        # If FAQ confidence is low - don't auto-reply, ask the human
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
                "reply_subject": payload.get("reply_subject", ""),
            })
            notify_unknown_question_email(lead_email, client_id, reply_text, iid)
            logger.info(f"QUESTION with low FAQ confidence ({faq_confidence}) for {lead_email} - waiting for human")
            return {"status": "waiting_for_human", "interaction_id": iid, "reason": "FAQ no match"}

    # 12. Generate reply
    # Atskiriam naują prospect'o žinutę nuo thread history (cold email + ankstesnės žinutės).
    # Taip LLM mato FULL kontekstą ir gali atpažinti "reply į CTA" scenarijus.
    current_reply, thread_history = _split_reply_and_history(reply_text)
    try:
        agent_reply = await generate_reply(
            prospect_message=current_reply or reply_text,  # fallback į full jei split'as nepavyko
            classification=classification.category,
            client_config=client_config,
            few_shots=few_shots,
            anti_patterns=anti_patterns,
            available_slots=available_slots,
            matching_faq=matching_faq,
            thread_position=thread_position,
            thread_history=thread_history,
            target_language=original_language,
        )
    except APIUnavailableError as e:
        logger.error(f"Claude API unavailable during reply generation for {lead_email}: {e}")
        await notify_error("claude_api_unavailable", f"Reply generation failed for {lead_email}: {e}")
        await notify_escalation(lead_email, payload.get("campaign_name", ""),
                               "Claude API nepasiekiamas - atsakymo generavimas nepavyko", reply_text)
        await log_interaction(db, {
            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
            "lead_email": lead_email, "email_account": payload.get("email_account"),
            "email_id": email_id, "client_id": client_id,
            "prospect_message": reply_text, "classification": classification.category,
            "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
            "was_sent": False, "thread_position": thread_position,
            "reply_subject": payload.get("reply_subject", ""),
        })
        return {"status": "error", "reason": f"Claude API unavailable: {e}"}

    # 12a. Hallucination guard - deterministinis regex check'as prieš LLM quality review
    hallucination_issues = check_hallucinations(agent_reply, client_config)
    if hallucination_issues:
        logger.warning(f"Hallucination guard triggered for {lead_email}: {hallucination_issues}")

    # 12b. Quality review
    quality = await review_quality(
        prospect_message=reply_text,
        classification=classification.category,
        generated_reply=agent_reply,
        client_name=client_config.get("client_name", client_id),
    )
    # Force-fail jei halucinacijos - nepaisant LLM opinion'o
    if hallucination_issues:
        quality.passed = False
        quality.score = min(quality.score, 3)
        quality.issues = hallucination_issues + list(quality.issues)
        quality.summary = f"[Hallucination guard] {'; '.join(hallucination_issues[:2])} | LLM: {quality.summary}"
    logger.info(f"Quality review for {lead_email}: score={quality.score}/10 passed={quality.passed}")

    if not quality.passed:
        if config.TEST_MODE:
            log_test_reply(
                campaign_name=payload.get("campaign_name", ""), client_id=client_id,
                lead_email=lead_email, company=payload.get("company_name", ""),
                original_message=reply_text, classification=classification.category,
                confidence=classification.confidence,
                generated_reply=agent_reply,
                sending_account=payload.get("email_account", ""),
                status=f"quality_failed ({quality.score}/10)",
            )
        notify_escalation_email(lead_email, client_id, classification.category,
                               classification.confidence, reply_text,
                               f"Quality check failed ({quality.score}/10): {quality.summary}")
        await notify_escalation(lead_email, payload.get("campaign_name", ""),
                               f"quality failed ({quality.score}/10): {'; '.join(quality.issues[:2])}", reply_text)
        iid = await log_interaction(db, {
            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
            "lead_email": lead_email, "email_account": payload.get("email_account"),
            "email_id": email_id, "client_id": client_id,
            "prospect_message": reply_text, "classification": classification.category,
            "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
            "agent_reply": agent_reply, "was_sent": False, "thread_position": thread_position,
            "quality_score": quality.score, "quality_issues": json.dumps(quality.issues, ensure_ascii=False),
            "quality_summary": quality.summary,
            "improvement_suggestion": getattr(quality, "improvement_suggestion", "") or "",
            "reply_subject": payload.get("reply_subject", ""),
        })
        return {"status": "quality_failed", "interaction_id": iid, "quality_score": quality.score}

    # Translate prospect + draft to LT for dashboard display (no-op if original_language=="lt")
    prospect_message_lt = await translate_to_lt(reply_text, original_language)
    agent_reply_lt = await translate_to_lt(agent_reply, original_language)

    # If approval required, log as pending and notify - DO NOT auto-send
    if approval_required:
        iid = await log_interaction(db, {
            "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
            "lead_email": lead_email, "email_account": payload.get("email_account"),
            "email_id": email_id, "client_id": client_id,
            "prospect_message": reply_text, "classification": classification.category,
            "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
            "agent_reply": agent_reply, "was_sent": False,
            "matched_faq_index": matched_faq_index, "faq_confidence": faq_confidence,
            "offered_slots": offered_slots_json,
            "few_shots_used": json.dumps([fs["id"] for fs in few_shots]) if few_shots else None,
            "thread_position": thread_position,
            "quality_score": quality.score,
            "quality_issues": json.dumps(quality.issues, ensure_ascii=False),
            "quality_summary": quality.summary,
            "improvement_suggestion": getattr(quality, "improvement_suggestion", "") or "",
            "approval_status": "pending",
            "original_language": original_language,
            "prospect_message_lt": prospect_message_lt,
            "agent_reply_lt": agent_reply_lt,
            "reply_subject": payload.get("reply_subject", ""),
        })
        await notify_approval_pending(
            iid=iid,
            lead_email=lead_email,
            client_id=client_id,
            classification=classification.category,
            quality_score=quality.score,
            confidence=classification.confidence,
            prospect_message_lt=prospect_message_lt or reply_text,
            agent_reply_lt=agent_reply_lt or agent_reply,
            original_language=original_language,
            dashboard_base_url=config.DASHBOARD_BASE_URL,
        )
        return {"status": "pending_approval", "interaction_id": iid}

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
            status=f"test_mode (quality: {quality.score}/10)",
        )
        if classification.category == "INTERESTED":
            notify_interested_email(lead_email, client_id, reply_text, agent_reply)
        was_sent = False
    else:
        # Auto-detect attachments based on agent reply text + prospect language
        # Use simple fallback heuristic (more reliable than langdetect for short LT replies)
        prospect_lang = detect_language_from_text(reply_text)

        try:
            attachments_to_send = detect_attachments(client_config, agent_reply, prospect_lang)
        except Exception as e:
            logger.error(f"attachment detection failed: {e}")
            attachments_to_send = []

        try:
            await send_reply(
                payload.get("email_account", ""),
                email_id,
                payload.get("reply_subject", ""),
                agent_reply,
                attachments=attachments_to_send or None,
            )
            if attachments_to_send:
                logger.info(f"Sent reply with {len(attachments_to_send)} attachment(s) to {lead_email}")
        except Exception as e:
            await notify_error("instantly_send_failed", str(e))
            await log_interaction(db, {
                "campaign_id": campaign_id, "campaign_name": payload.get("campaign_name"),
                "lead_email": lead_email, "email_account": payload.get("email_account"),
                "email_id": email_id, "client_id": client_id,
                "prospect_message": reply_text, "classification": classification.category,
                "confidence": classification.confidence, "classification_reasoning": classification.reasoning,
                "agent_reply": agent_reply, "was_sent": False, "thread_position": thread_position,
                "reply_subject": payload.get("reply_subject", ""),
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
        "quality_score": quality.score, "quality_issues": json.dumps(quality.issues, ensure_ascii=False),
        "quality_summary": quality.summary,
        "improvement_suggestion": getattr(quality, "improvement_suggestion", "") or "",
        "original_language": original_language,
        "prospect_message_lt": prospect_message_lt,
        "agent_reply_lt": agent_reply_lt,
        "reply_subject": payload.get("reply_subject", ""),
    })

    # 15. Slack notification
    await notify_reply_sent(lead_email, payload.get("campaign_name", ""), classification.category, classification.confidence, reply_text, agent_reply)

    return {"status": "test_mode_logged" if config.TEST_MODE else "sent", "interaction_id": iid}
