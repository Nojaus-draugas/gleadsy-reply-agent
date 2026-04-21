"""Pauliaus stiliaus analizė iš jo realių atsakymų DB'je (thumbs_up few-shots).

Išveda signalus kuriuos LLM gali naudoti kaip instrukciją kaip rašyti panašiai:
- Sign-off dažnumas ("Linkėjimai" vs "Pagarbiai" vs "Cordialement")
- Emoji naudojimas (";)", ":)", ":D")
- Vidutinis sakinių skaičius
- Dažniausios pradžios frazės ("Ačiū už atsakymą", "Super", "Supratau")
- Pauliaus kvalifikacijos klausimų pavyzdžiai
"""
import re
import logging
from collections import Counter
import aiosqlite

logger = logging.getLogger(__name__)


def _extract_first_sentence(text: str) -> str:
    """Grąžina pirmą sakinį (iki . ! ? ar \\n)."""
    if not text:
        return ""
    m = re.search(r"^[^.!?\n]+[.!?]", text.strip())
    return m.group(0).strip() if m else text.split("\n")[0].strip()


def _count_sentences(text: str) -> int:
    """Aproksimuoja sakinių kiekį tekste."""
    if not text:
        return 0
    # Pašalin em-dash -> - ir bullet'ai
    cleaned = re.sub(r"[→•-]\s*", "", text)
    sentences = re.split(r"[.!?]+", cleaned)
    return len([s for s in sentences if s.strip()])


async def analyze_paulius_style(conn: aiosqlite.Connection, client_id: str | None = None) -> dict:
    """Analyzuoja VISUS thumbs_up few-shots ir grąžina style profile."""
    conn.row_factory = aiosqlite.Row

    sql = ("SELECT agent_reply FROM interactions "
           "WHERE agent_reply IS NOT NULL AND human_rating = 'thumbs_up' "
           "AND was_sent = 1")
    params = []
    if client_id:
        sql += " AND client_id = ?"
        params.append(client_id)
    sql += " ORDER BY created_at DESC LIMIT 100"

    cursor = await conn.execute(sql, params)
    replies = [dict(r)["agent_reply"] for r in await cursor.fetchall()]

    if not replies:
        return {"total_samples": 0}

    sign_offs = Counter()
    emojis = Counter()
    first_phrases = Counter()
    sentence_counts = []

    sign_off_patterns = [
        ("Linkėjimai", r"Linkėjimai[,\s]"),
        ("Pagarbiai", r"Pagarbiai[,\s]"),
        ("Cordialement", r"Cordialement[,\s]"),
        ("Best regards", r"Best regards[,\s]"),
    ]
    emoji_patterns = [";)", ":)", ":D", ":(", ";-)", ":-)"]
    common_first_phrases = [
        "Ačiū už atsakymą", "Super", "Supratau", "Puiku", "Sveiki",
        "Parfait", "Bonjour", "Aišku", "Smagu", "Malonu",
    ]

    for reply in replies:
        # Sign-off
        for name, pat in sign_off_patterns:
            if re.search(pat, reply, re.IGNORECASE):
                sign_offs[name] += 1
                break
        # Emojis
        for em in emoji_patterns:
            if em in reply:
                emojis[em] += reply.count(em)
        # Pradžios frazės
        first = _extract_first_sentence(reply)
        for phrase in common_first_phrases:
            if first.lower().startswith(phrase.lower()):
                first_phrases[phrase] += 1
                break
        # Sakinių kiekis
        sentence_counts.append(_count_sentences(reply))

    avg_sentences = sum(sentence_counts) / len(sentence_counts) if sentence_counts else 0

    return {
        "total_samples": len(replies),
        "sign_offs": dict(sign_offs.most_common()),
        "top_sign_off": sign_offs.most_common(1)[0][0] if sign_offs else None,
        "emojis": dict(emojis.most_common()),
        "uses_emojis_pct": round(100 * sum(1 for r in replies if any(e in r for e in emoji_patterns)) / len(replies), 1),
        "first_phrases": dict(first_phrases.most_common()),
        "avg_sentences": round(avg_sentences, 1),
        "min_sentences": min(sentence_counts) if sentence_counts else 0,
        "max_sentences": max(sentence_counts) if sentence_counts else 0,
    }


async def learning_progress(conn: aiosqlite.Connection, days: int = 30) -> dict:
    """Grąžina mokymosi metrikų istoriją - ar score gerėja per laiką."""
    conn.row_factory = aiosqlite.Row

    # Weekly breakdown - average quality score per week
    cursor = await conn.execute(
        """SELECT strftime('%Y-W%W', created_at) as week,
                  COUNT(*) as total,
                  AVG(quality_score) as avg_score,
                  SUM(CASE WHEN human_rating='thumbs_up' THEN 1 ELSE 0 END) as thumbs_up,
                  SUM(CASE WHEN human_rating='thumbs_down' THEN 1 ELSE 0 END) as thumbs_down,
                  SUM(CASE WHEN outcome='meeting_booked' THEN 1 ELSE 0 END) as meetings
           FROM interactions
           WHERE quality_score IS NOT NULL
             AND created_at >= datetime('now', ?)
           GROUP BY week ORDER BY week DESC LIMIT 12""",
        (f"-{days} days",),
    )
    weekly = [dict(r) for r in await cursor.fetchall()]

    # Total stats
    cursor = await conn.execute(
        "SELECT COUNT(*) as fs_count FROM interactions "
        "WHERE human_rating = 'thumbs_up' AND was_sent = 1"
    )
    fs_count = dict(await cursor.fetchone())["fs_count"]

    cursor = await conn.execute(
        "SELECT COUNT(*) as override_count FROM interactions "
        "WHERE human_override_text IS NOT NULL"
    )
    override_count = dict(await cursor.fetchone())["override_count"]

    return {
        "few_shot_bank_size": fs_count,
        "total_overrides_from_paulius": override_count,
        "weekly_trend": weekly,
    }
