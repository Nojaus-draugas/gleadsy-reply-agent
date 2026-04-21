"""Auto-learn loop: sistema iš Pauliaus realiai išsiųstų atsakymų mokosi stiliaus.

Veikimas:
1. Kas N valandų: traukia iš Instantly visas Pauliaus SENT žinutes nuo paskutinio run'o.
2. Kiekvienai SENT žinutei:
   - Ieško prospect'o ankstesnio reply (in_reply_to / thread matching).
   - Jei yra match interaction DB'je:
     a) Jei `agent_reply` yra ir skiriasi nuo Pauliaus body - tai OVERRIDE signalas
        (Paulius pats perrašė). Saugoma `human_override_text` + `human_rating='thumbs_down'`
        su reasoning 'Paulius perraso Instantly'.
     b) Jei `agent_reply` neegzistuoja (arba sutampa) - saugoma kaip naujas
        few-shot pavyzdys: naujas INSERT į interactions su `human_rating='thumbs_up'`,
        `outcome='meeting_booked'`, `agent_reply=Paulius atsakymas`.
3. Per laiką DB auga Pauliaus pavyzdžių kolekcija - agent'as mokosi iš jo stiliaus.
"""
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import aiosqlite

import config
from core.instantly_client import poll_sent_emails
from core.client_loader import get_client_by_campaign

logger = logging.getLogger(__name__)

# State failas paskutinio run'o timestamp'ui
_STATE_FILE = Path(config.BASE_DIR) / "data" / "auto_learn_state.json"


def _load_last_run() -> str:
    """Grąžina ISO timestamp paskutinio run'o. Pirmas kartas - 24h atgal."""
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text())
            return data.get("last_run", "")
        except Exception:
            pass
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()


def _save_last_run(ts: str) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps({"last_run": ts}))


def _clean_quoted_history(body_text: str) -> str:
    """Iškerpa email quote history (>, From:, On DATE wrote) palieka TIK naują
    Pauliaus parašytą turinį. Normalizuoja kad galėtum palyginti su agent_reply."""
    if not body_text:
        return ""
    lines = body_text.split("\n")
    clean = []
    for line in lines:
        stripped = line.strip()
        # Quote markers
        if stripped.startswith(">") or stripped.startswith("|"):
            break
        # Common email quote headers
        if re.match(r"^(On\s.+wrote:|Le\s.+écrit\s:|\d{4}-\d{2}-\d{2}.+:|From:|Nuo:|Išsiųsta:|-----Original)", stripped):
            break
        clean.append(line)
    return "\n".join(clean).strip()


def _normalize_for_compare(text: str) -> str:
    """Normalizuoja tekstą palyginimui: pašalina whitespace, signature'ą."""
    if not text:
        return ""
    # Nutraukia po signature indicators
    for marker in ["\nLinkėjimai", "\nLinkejimai", "\nPagarbiai", "\nCordialement", "\nBest regards", "\n--\n", "\n-- \n"]:
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx]
    # Kollapse whitespace
    return re.sub(r"\s+", " ", text).strip().lower()


async def run_auto_learn(conn: aiosqlite.Connection, clients: dict) -> dict:
    """Pagrindinė funkcija - iškviečiama iš scheduler'io.

    Grąžina dict'ą su statistikomis: {polled, new_few_shots, overrides, skipped}.
    """
    stats = {"polled": 0, "new_few_shots": 0, "overrides": 0, "skipped": 0}
    conn.row_factory = aiosqlite.Row

    since = _load_last_run()
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        sent_emails = await poll_sent_emails(since)
    except Exception as e:
        logger.error(f"auto_learn: poll_sent_emails failed: {e}")
        return stats

    stats["polled"] = len(sent_emails)
    if not sent_emails:
        _save_last_run(now_iso)
        return stats

    for sent in sent_emails:
        try:
            lead_email = sent.get("lead_email", "")
            campaign_id = sent.get("campaign_id", "")
            body_text = _clean_quoted_history(sent.get("body_text", ""))
            reply_to_uuid = sent.get("reply_to_uuid", "")
            subject = (sent.get("subject") or "").lower()

            if not lead_email or not body_text:
                stats["skipped"] += 1
                continue

            # Filter: jei subject nepradeda "Re:" ar "RE:" - tai cold outreach/follow-up, ne reply
            # Praleidžiam tokius - jie yra auto-generated cold email'ai, ne Pauliaus personalized reply'ai
            if not subject.startswith("re:") and not subject.startswith("re :") and not subject.startswith("fwd:"):
                stats["skipped"] += 1
                continue

            # KRITIŠKAI: jei yra sequence step - tai šablono follow-up, NE personalizuotas Pauliaus reply.
            # Instantly step'ai: "0_0_0" = cold open, "0_1_0" = follow-up #1, etc.
            # Pauliaus rankomis rašytas reply NETURI step'o (null/empty).
            step = sent.get("step") or ""
            if step and step.strip():
                stats["skipped"] += 1
                continue

            # Filter: labai trumpi atsakymai (< 20 char) - dažniausiai "Sveiki," ar confirmation
            if len(body_text.strip()) < 20:
                stats["skipped"] += 1
                continue

            # Skip jei jau saugota (dedup per from_account+timestamp arba email_id)
            sent_id = sent.get("email_id", "")
            cur = await conn.execute(
                "SELECT id FROM interactions WHERE email_id = ? LIMIT 1",
                (f"paulius-sent-{sent_id}",),
            )
            if await cur.fetchone():
                stats["skipped"] += 1
                continue

            # Client lookup
            client_config = get_client_by_campaign(clients, campaign_id) if campaign_id else None
            client_id = client_config.get("client_id") if client_config else "unknown"

            # Ieško ankstesnio interaction kuris atitinka - ar Paulius override'ino agent'o draftą?
            if reply_to_uuid:
                cur = await conn.execute(
                    "SELECT id, agent_reply, classification FROM interactions "
                    "WHERE email_id = ? AND lead_email = ? LIMIT 1",
                    (reply_to_uuid, lead_email),
                )
                prev = await cur.fetchone()
            else:
                cur = await conn.execute(
                    "SELECT id, agent_reply, classification FROM interactions "
                    "WHERE lead_email = ? AND campaign_id = ? ORDER BY created_at DESC LIMIT 1",
                    (lead_email, campaign_id),
                )
                prev = await cur.fetchone()

            if prev and prev["agent_reply"]:
                # Palygink Pauliaus realų reply su agent_reply
                agent_norm = _normalize_for_compare(prev["agent_reply"])
                paulius_norm = _normalize_for_compare(body_text)

                if agent_norm and paulius_norm and agent_norm != paulius_norm:
                    # OVERRIDE - Paulius perrašė. Žymim prev interaction'ą su override_text.
                    await conn.execute(
                        "UPDATE interactions SET human_override_text = ?, "
                        "human_rating = 'thumbs_down', "
                        "human_feedback_note = 'Paulius perrase Instantly - auto-learn' "
                        "WHERE id = ? AND human_rating IS NULL",
                        (body_text, prev["id"]),
                    )
                    stats["overrides"] += 1
                    logger.info(f"auto_learn: override signal for interaction {prev['id']} (Paulius {lead_email})")

            # Saugom Pauliaus atsakymą kaip naują thumbs_up few-shot
            classification = prev["classification"] if prev else "INTERESTED"
            await conn.execute(
                """INSERT INTO interactions
                (campaign_id, campaign_name, lead_email, email_id, client_id,
                 prospect_message, classification, confidence, classification_reasoning,
                 agent_reply, was_sent, thread_position, human_rating, outcome, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign_id or "auto-learn", "auto-learn-paulius-sent",
                    lead_email, f"paulius-sent-{sent_id}", client_id,
                    "(auto-learn: prospect context not captured)",
                    classification, 1.0,
                    "auto-learn: Paulius realiai isssiuste is Instantly",
                    body_text, 1, 1, "thumbs_up", "replied_again",
                    sent.get("timestamp") or datetime.utcnow().isoformat(),
                ),
            )
            stats["new_few_shots"] += 1
        except Exception as e:
            logger.warning(f"auto_learn: row failed: {e}")
            stats["skipped"] += 1

    await conn.commit()
    _save_last_run(now_iso)
    logger.info(
        f"auto_learn done: polled={stats['polled']} new_few_shots={stats['new_few_shots']} "
        f"overrides={stats['overrides']} skipped={stats['skipped']}"
    )
    return stats
