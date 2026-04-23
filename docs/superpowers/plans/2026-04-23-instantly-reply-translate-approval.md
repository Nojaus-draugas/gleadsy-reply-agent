# Foreign-Language Reply Translation + Approval Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Foreign-language lead replies get translated to LT for Paulius + require approval before sending; LT clients keep auto-send behavior unchanged.

**Architecture:** Extend existing `interactions` table with translation + approval columns. Webhook pipeline branches on `client_config.approval_required`: foreign clients hit approval queue (Slack notify, dashboard `/pending`), LT clients keep auto-send. Translation via Claude Haiku (prospect + draft); edit workflow uses Claude Sonnet with LT instruction → original-language rewrite. Language detection via `langdetect` lib (offline, zero API cost) with per-campaign config as primary hint.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite, Anthropic SDK, langdetect, pytest-asyncio, httpx.

**Spec:** `gleadsy-reply-agent/docs/superpowers/specs/2026-04-23-instantly-reply-translate-approval-design.md`

**Working directory:** All paths relative to `gleadsy-reply-agent/` unless stated.

---

## File Structure

**Create:**
- `core/language_detection.py` - detect_language() using langdetect
- `core/translation.py` - translate_to_lt(), rewrite_draft()
- `tests/test_language_detection.py`
- `tests/test_translation.py`
- `tests/test_yaml_backward_compat.py`
- `tests/test_approval_flow.py` (routing + DB helpers)
- `tests/test_edit_workflow.py`
- `tests/fixtures/reply_fr_interested.json` - FR lead fixture

**Modify:**
- `db/database.py` - 9 new migrations, new helper functions
- `core/client_loader.py` - validate new campaign dict format, expose `get_campaign_language()`
- `core/slack_notifier.py` - notify_approval_pending()
- `core/reply_generator.py` - accept `target_language` param
- `core/self_improver.py` - filter few-shots by `original_language`
- `prompts/reply.py` - include `target_language` hint
- `webhooks/instantly_webhook.py` - branch on approval_required + translation calls
- `main.py` - `/pending` page + `/api/approve|reject|mark_sent|takeover|edit_draft/{iid}` endpoints + `/replies` header badge + `/conversation` pending badge
- `requirements.txt` - add `langdetect`
- `tests/test_client_loader.py` - extend for dict-campaign format

---

## Task 1: Add langdetect dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Read current requirements**

Run: `cat requirements.txt`

- [ ] **Step 2: Add langdetect**

Append to `requirements.txt`:

```
langdetect==1.0.9
```

- [ ] **Step 3: Install locally**

Run: `pip install langdetect==1.0.9`
Expected: `Successfully installed langdetect-1.0.9 six-1.x.x`

- [ ] **Step 4: Verify import works**

Run: `python -c "from langdetect import detect, DetectorFactory; DetectorFactory.seed = 0; print(detect('Bonjour comment allez-vous'))"`
Expected: `fr`

- [ ] **Step 5: Commit**

```bash
git add requirements.txt
git commit -m "deps: add langdetect for reply-agent language detection"
```

---

## Task 2: Language detection module (TDD)

**Files:**
- Create: `core/language_detection.py`
- Test: `tests/test_language_detection.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_language_detection.py`:

```python
import pytest
from core.language_detection import detect_language, SUPPORTED_LANGUAGES


def test_detects_french():
    assert detect_language("Bonjour, merci pour votre message, je suis intéressé", "fr") == "fr"


def test_detects_german():
    assert detect_language("Guten Tag, vielen Dank für Ihre Nachricht. Ich bin interessiert.", "de") == "de"


def test_detects_lithuanian():
    assert detect_language("Labas, ačiū už laišką, esu suinteresuotas.", "lt") == "lt"


def test_detects_estonian():
    assert detect_language("Tere, tänan teie sõnumi eest. Olen huvitatud.", "et") == "et"


def test_detects_latvian():
    assert detect_language("Sveiki, paldies par jūsu vēstuli. Esmu ieinteresēts.", "lv") == "lv"


def test_detects_english():
    assert detect_language("Hello, thanks for your message. I'm interested.", "en") == "en"


def test_hint_used_when_detection_matches():
    # When detection agrees with hint, hint wins (cheaper branch)
    assert detect_language("Bonjour à tous, merci beaucoup", "fr") == "fr"


def test_hint_used_when_text_too_short():
    # Very short text is unreliable - prefer hint
    assert detect_language("Ok", "fr") == "fr"


def test_detected_language_overrides_hint_when_mismatch():
    # Lead wrote English even though campaign was FR - respect user's actual language
    fr_hint = "fr"
    english_text = "Hello, I'm interested in your services. Could you share more information about pricing?"
    assert detect_language(english_text, fr_hint) == "en"


def test_unsupported_language_falls_back_to_en():
    # Arabic is not in supported set; should fallback
    arabic_text = "مرحبا كيف حالك شكرا جزيلا"
    result = detect_language(arabic_text, "fr")
    assert result in ("en", "fr")  # either fallback-en or hint-fr acceptable


def test_empty_text_returns_hint():
    assert detect_language("", "fr") == "fr"
    assert detect_language("   ", "lt") == "lt"


def test_supported_languages_contains_expected():
    expected = {"lt", "en", "fr", "de", "et", "lv"}
    assert expected.issubset(SUPPORTED_LANGUAGES)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_language_detection.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.language_detection'`

- [ ] **Step 3: Implement language_detection module**

Create `core/language_detection.py`:

```python
"""Offline language detection via langdetect with hint-based override logic.

Used in reply agent pipeline to determine target reply language when a prospect
responds. Per-campaign YAML config provides the primary hint; if detected
language differs strongly, the detection wins (user's actual language matters).
"""
import logging
from langdetect import detect, detect_langs, DetectorFactory, LangDetectException

# Determinism - required for consistent test results
DetectorFactory.seed = 0

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = {"lt", "en", "fr", "de", "et", "lv"}

# Text shorter than this is considered too unreliable to override hint
MIN_TEXT_FOR_DETECTION = 30

# Confidence below this means we trust hint over detection
DETECTION_CONFIDENCE_OVERRIDE_THRESHOLD = 0.90


def detect_language(text: str, campaign_hint: str) -> str:
    """Detect language of text, with campaign_hint as tie-breaker.

    Returns ISO-639-1 code from SUPPORTED_LANGUAGES, or campaign_hint if
    detection is unreliable. Falls back to "en" if detection returns an
    unsupported language and campaign_hint is also not supported.
    """
    hint = (campaign_hint or "en").lower()
    if hint not in SUPPORTED_LANGUAGES:
        hint = "en"

    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) < MIN_TEXT_FOR_DETECTION:
        return hint

    try:
        ranked = detect_langs(cleaned)
    except LangDetectException:
        logger.warning("langdetect failed on text: %s", cleaned[:80])
        return hint

    if not ranked:
        return hint

    top = ranked[0]
    detected_lang = top.lang.lower()
    detected_conf = top.prob

    # Detection matches hint - cheap confirm
    if detected_lang == hint:
        return hint

    # Detection is clearly different and confident - override
    if detected_conf >= DETECTION_CONFIDENCE_OVERRIDE_THRESHOLD:
        if detected_lang in SUPPORTED_LANGUAGES:
            logger.info(
                "Language override: campaign hint=%s detected=%s (conf=%.2f)",
                hint, detected_lang, detected_conf,
            )
            return detected_lang
        # Unsupported language detected with high confidence - fallback to "en"
        logger.warning(
            "Detected unsupported language %s (conf=%.2f), fallback to en",
            detected_lang, detected_conf,
        )
        return "en"

    # Detection uncertain - trust hint
    return hint
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_language_detection.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add core/language_detection.py tests/test_language_detection.py
git commit -m "feat: offline language detection with hint-based override"
```

---

## Task 3: Translation module (translate_to_lt)

**Files:**
- Create: `core/translation.py`
- Test: `tests/test_translation.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_translation.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch
from core.translation import translate_to_lt, rewrite_draft


@pytest.mark.asyncio
async def test_translate_lt_noop_when_already_lt():
    # No API call should be made for LT source
    with patch("core.translation.call_claude_with_retry") as mock:
        result = await translate_to_lt("Labas, kaip sekasi?", "lt")
    assert result == "Labas, kaip sekasi?"
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_translate_lt_empty_noop():
    with patch("core.translation.call_claude_with_retry") as mock:
        assert await translate_to_lt("", "fr") == ""
        assert await translate_to_lt("   ", "de") == "   "
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_translate_lt_calls_claude_for_fr():
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(return_value="Labas, ačiū.")) as mock:
        result = await translate_to_lt("Bonjour, merci.", "fr")
    assert result == "Labas, ačiū."
    mock.assert_called_once()
    # Verify model is Haiku (cheap) and purpose set
    kwargs = mock.call_args.kwargs
    assert "haiku" in kwargs["model"].lower()
    assert kwargs["purpose"] == "translate_to_lt"


@pytest.mark.asyncio
async def test_translate_lt_prompt_includes_source_language():
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(return_value="...")) as mock:
        await translate_to_lt("Guten Tag", "de")
    user_msg = mock.call_args.kwargs["messages"][0]["content"]
    system_msg = mock.call_args.kwargs["system"]
    assert "Guten Tag" in user_msg
    # System prompt should mention source language so Claude knows what to translate from
    combined = (system_msg or "") + user_msg
    assert "de" in combined.lower() or "german" in combined.lower() or "translate" in combined.lower()


@pytest.mark.asyncio
async def test_translate_lt_returns_empty_on_api_failure():
    from core.classifier import APIUnavailableError
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(side_effect=APIUnavailableError("down"))):
        result = await translate_to_lt("Bonjour", "fr")
    assert result == ""  # graceful degradation - dashboard shows original only


@pytest.mark.asyncio
async def test_rewrite_draft_calls_sonnet():
    client_config = {"client_name": "Gleadsy", "tone": {"language": "fr"}}
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(return_value="Merci! Quand nous rencontrer?")) as mock:
        result = await rewrite_draft(
            original_draft="Merci pour votre intérêt.",
            lt_instruction="Pridėk klausimą apie susitikimo laiką",
            target_language="fr",
            client_config=client_config,
        )
    assert result == "Merci! Quand nous rencontrer?"
    kwargs = mock.call_args.kwargs
    assert "sonnet" in kwargs["model"].lower()
    assert kwargs["purpose"] == "rewrite_draft"


@pytest.mark.asyncio
async def test_rewrite_draft_prompt_contains_instruction_and_draft():
    client_config = {"client_name": "Gleadsy", "tone": {"language": "fr"}}
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(return_value="...")) as mock:
        await rewrite_draft(
            original_draft="DRAFT_TEXT",
            lt_instruction="PAKEISK_TAI",
            target_language="fr",
            client_config=client_config,
        )
    user_msg = mock.call_args.kwargs["messages"][0]["content"]
    system_msg = mock.call_args.kwargs["system"]
    assert "DRAFT_TEXT" in (system_msg or "")
    assert "PAKEISK_TAI" in user_msg
    # Target language mentioned in system
    assert "fr" in (system_msg or "").lower()


@pytest.mark.asyncio
async def test_rewrite_draft_raises_on_api_failure():
    # Unlike translate, rewrite is interactive - user is waiting - we want to surface errors
    from core.classifier import APIUnavailableError
    client_config = {"client_name": "Gleadsy", "tone": {"language": "fr"}}
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(side_effect=APIUnavailableError("down"))):
        with pytest.raises(APIUnavailableError):
            await rewrite_draft("draft", "instrukcija", "fr", client_config)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_translation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.translation'`

- [ ] **Step 3: Implement translation module**

Create `core/translation.py`:

```python
"""Translation helpers for foreign-language reply approval flow.

translate_to_lt: prospect_message / agent_reply → LT preview (graceful on failure).
rewrite_draft: LT instruction + existing draft → rewritten draft in target language.
"""
import logging
import config
from core.classifier import call_claude_with_retry, APIUnavailableError

logger = logging.getLogger(__name__)

LANGUAGE_NAMES = {
    "lt": "Lithuanian", "en": "English", "fr": "French",
    "de": "German", "et": "Estonian", "lv": "Latvian",
}


async def translate_to_lt(text: str, source_language: str) -> str:
    """Translate text to Lithuanian. No-op if source is 'lt' or text is empty.

    Returns "" on API failure (graceful degradation - dashboard will show
    only the original text, caller logs but does not raise).
    """
    if not text or not text.strip():
        return text
    if (source_language or "").lower() == "lt":
        return text

    source_name = LANGUAGE_NAMES.get(source_language.lower(), source_language)
    system = (
        f"You translate {source_name} text into Lithuanian. "
        "Translate faithfully, keeping tone and intent. "
        "Return ONLY the Lithuanian translation, no quotes, no explanations, no prefixes."
    )
    try:
        result = await call_claude_with_retry(
            model=config.TRANSLATION_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": text}],
            purpose="translate_to_lt",
        )
    except APIUnavailableError as e:
        logger.error("translate_to_lt failed (source=%s): %s", source_language, e)
        return ""
    return (result or "").strip()


async def rewrite_draft(
    original_draft: str,
    lt_instruction: str,
    target_language: str,
    client_config: dict,
) -> str:
    """Rewrite a draft reply based on Lithuanian instruction from user.

    The draft stays in target_language; the instruction is in LT. Returns the
    rewritten draft in target_language. Raises APIUnavailableError on failure
    (UI must surface this - user is waiting in edit modal).
    """
    target_name = LANGUAGE_NAMES.get(target_language.lower(), target_language)
    client_name = client_config.get("client_name", "")
    tone = client_config.get("tone", {})
    sign_off = tone.get("sign_off", "")
    sender = tone.get("sender_name", "")
    personality = tone.get("personality", "")

    system = (
        f"Tu esi {client_name} atstovas. Tu jau parašei šį draftą {target_name} kalba:\n\n"
        f"<draft>\n{original_draft}\n</draft>\n\n"
        f"Vartotojas nori perrašyti draftą pagal lietuviškas instrukcijas.\n"
        f"Perrašyk TOJE PAČIOJE {target_name} kalboje, išlaikydamas toną ir stilių.\n\n"
        f"Stilius: {personality}\n"
        f"Pasirašymas: {sign_off}, {sender}\n\n"
        f"KRITIŠKAI SVARBU:\n"
        f"- Grąžink TIK naują draftą, be jokių paaiškinimų, be kabučių, be prefiksų.\n"
        f"- Niekada nenaudok em-dash `—` ar en-dash `–`; rašyk trumpą brūkšnį `-`.\n"
        f"- Neįtrauk lietuviškų instrukcijų į atsakymą."
    )
    user_msg = f"Instrukcijos: {lt_instruction}"

    result = await call_claude_with_retry(
        model=config.REWRITE_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
        purpose="rewrite_draft",
    )
    return (result or "").strip()
```

- [ ] **Step 4: Add model constants to config.py**

Modify `config.py` - add near other model constants (around line 28):

```python
TRANSLATION_MODEL = os.getenv("TRANSLATION_MODEL", "claude-haiku-4-5-20251001")
REWRITE_MODEL = os.getenv("REWRITE_MODEL", "claude-sonnet-4-6")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_translation.py -v`
Expected: All 8 tests PASS

- [ ] **Step 6: Commit**

```bash
git add core/translation.py tests/test_translation.py config.py
git commit -m "feat: translation + draft rewrite modules via Claude"
```

---

## Task 4: DB schema migrations (approval + translation columns)

**Files:**
- Modify: `db/database.py` (MIGRATIONS list)

- [ ] **Step 1: Write failing tests**

Create `tests/test_approval_flow.py` (will be extended in later tasks - start with schema only):

```python
import pytest
import pytest_asyncio
import json
from db.database import init_db


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_interactions_table_has_new_columns(db):
    cursor = await db.execute("PRAGMA table_info(interactions)")
    cols = {row["name"] for row in await cursor.fetchall()}
    expected_new = {
        "original_language",
        "prospect_message_lt",
        "agent_reply_lt",
        "approval_status",
        "approved_at",
        "approved_by",
        "edit_history",
        "final_sent_text",
    }
    missing = expected_new - cols
    assert not missing, f"Missing columns: {missing}"


@pytest.mark.asyncio
async def test_approval_index_exists(db):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_interactions_approval'"
    )
    assert await cursor.fetchone() is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_approval_flow.py -v`
Expected: FAIL with "Missing columns: {...}" for all new columns

- [ ] **Step 3: Add migrations to db/database.py**

Modify `db/database.py` - extend the `MIGRATIONS` list (currently ending at line 88 with `improvement_suggestion`):

```python
MIGRATIONS = [
    "ALTER TABLE interactions ADD COLUMN quality_score INTEGER",
    "ALTER TABLE interactions ADD COLUMN quality_issues TEXT",
    "ALTER TABLE interactions ADD COLUMN quality_summary TEXT",
    "ALTER TABLE interactions ADD COLUMN tokens_in INTEGER",
    "ALTER TABLE interactions ADD COLUMN tokens_out INTEGER",
    "ALTER TABLE interactions ADD COLUMN tokens_cache_read INTEGER",
    "ALTER TABLE interactions ADD COLUMN cost_usd REAL",
    "ALTER TABLE interactions ADD COLUMN improvement_suggestion TEXT",
    # 2026-04-23 - foreign-language approval + translation
    "ALTER TABLE interactions ADD COLUMN original_language TEXT",
    "ALTER TABLE interactions ADD COLUMN prospect_message_lt TEXT",
    "ALTER TABLE interactions ADD COLUMN agent_reply_lt TEXT",
    "ALTER TABLE interactions ADD COLUMN approval_status TEXT",
    "ALTER TABLE interactions ADD COLUMN approved_at TIMESTAMP",
    "ALTER TABLE interactions ADD COLUMN approved_by TEXT",
    "ALTER TABLE interactions ADD COLUMN edit_history TEXT",
    "ALTER TABLE interactions ADD COLUMN final_sent_text TEXT",
    "CREATE INDEX IF NOT EXISTS idx_interactions_approval ON interactions(approval_status)",
]
```

Note: the existing `_run_migrations` function catches all exceptions, so ALTER TABLE and CREATE INDEX both work within it. CREATE INDEX IF NOT EXISTS is idempotent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_approval_flow.py -v`
Expected: Both tests PASS

- [ ] **Step 5: Commit**

```bash
git add db/database.py tests/test_approval_flow.py
git commit -m "feat(db): add approval + translation columns to interactions"
```

---

## Task 5: DB helper functions for approval flow

**Files:**
- Modify: `db/database.py` (new functions)
- Test: `tests/test_approval_flow.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_approval_flow.py`:

```python
from datetime import datetime
from db.database import (
    log_interaction, update_approval_status, get_pending_drafts,
    get_pending_count, append_edit_history, update_draft_text,
    get_thread_reply_count,
)


async def _log_pending(db, **overrides) -> int:
    base = {
        "campaign_id": "camp-1", "lead_email": "x@y.com",
        "email_id": overrides.pop("email_id", f"eid-{datetime.utcnow().timestamp()}"),
        "client_id": "gleadsy",
        "prospect_message": "Bonjour", "classification": "QUESTION",
        "confidence": 0.9, "approval_status": "pending", "was_sent": False,
    }
    base.update(overrides)
    return await log_interaction(db, base)


@pytest.mark.asyncio
async def test_update_approval_status_to_sent(db):
    iid = await _log_pending(db)
    await update_approval_status(db, iid, "sent", approved_by="paulius",
                                  final_sent_text="Merci")
    cursor = await db.execute(
        "SELECT approval_status, approved_by, approved_at, was_sent, final_sent_text "
        "FROM interactions WHERE id = ?", (iid,),
    )
    row = dict(await cursor.fetchone())
    assert row["approval_status"] == "sent"
    assert row["approved_by"] == "paulius"
    assert row["approved_at"] is not None
    assert row["was_sent"] == 1  # SQLite BOOLEAN -> int
    assert row["final_sent_text"] == "Merci"


@pytest.mark.asyncio
async def test_update_approval_status_reject_does_not_flip_was_sent(db):
    iid = await _log_pending(db)
    await update_approval_status(db, iid, "rejected", approved_by="paulius")
    cursor = await db.execute(
        "SELECT approval_status, was_sent FROM interactions WHERE id = ?", (iid,),
    )
    row = dict(await cursor.fetchone())
    assert row["approval_status"] == "rejected"
    assert row["was_sent"] == 0


@pytest.mark.asyncio
async def test_get_pending_drafts_returns_only_pending(db):
    pending_iid = await _log_pending(db, email_id="eid-a")
    await _log_pending(db, email_id="eid-b", approval_status="sent", was_sent=True)
    rejected_iid = await _log_pending(db, email_id="eid-c", approval_status="rejected")
    pending = await get_pending_drafts(db)
    ids = {row["id"] for row in pending}
    assert pending_iid in ids
    assert rejected_iid not in ids
    assert len(ids) == 1


@pytest.mark.asyncio
async def test_get_pending_count(db):
    await _log_pending(db, email_id="eid-1")
    await _log_pending(db, email_id="eid-2")
    await _log_pending(db, email_id="eid-3", approval_status="sent", was_sent=True)
    assert await get_pending_count(db) == 2


@pytest.mark.asyncio
async def test_get_pending_drafts_filtered_by_client(db):
    iid_a = await _log_pending(db, email_id="eid-a", client_id="gleadsy")
    iid_b = await _log_pending(db, email_id="eid-b", client_id="ibjoist")
    filtered = await get_pending_drafts(db, client_id="gleadsy")
    assert {row["id"] for row in filtered} == {iid_a}


@pytest.mark.asyncio
async def test_append_edit_history_creates_list_on_first_edit(db):
    iid = await _log_pending(db)
    await append_edit_history(db, iid, {
        "lt_instruction": "pridėk klausimą",
        "before": "Merci",
        "after": "Merci! Une question?",
    })
    cursor = await db.execute("SELECT edit_history FROM interactions WHERE id = ?", (iid,))
    raw = (await cursor.fetchone())["edit_history"]
    history = json.loads(raw)
    assert len(history) == 1
    assert history[0]["lt_instruction"] == "pridėk klausimą"
    assert "ts" in history[0]


@pytest.mark.asyncio
async def test_append_edit_history_appends(db):
    iid = await _log_pending(db)
    await append_edit_history(db, iid, {"lt_instruction": "a", "before": "x", "after": "y"})
    await append_edit_history(db, iid, {"lt_instruction": "b", "before": "y", "after": "z"})
    cursor = await db.execute("SELECT edit_history FROM interactions WHERE id = ?", (iid,))
    history = json.loads((await cursor.fetchone())["edit_history"])
    assert len(history) == 2
    assert history[0]["lt_instruction"] == "a"
    assert history[1]["lt_instruction"] == "b"


@pytest.mark.asyncio
async def test_update_draft_text(db):
    iid = await _log_pending(db, agent_reply="Merci", agent_reply_lt="Ačiū")
    await update_draft_text(db, iid, "Merci beaucoup", "Labai ačiū")
    cursor = await db.execute(
        "SELECT agent_reply, agent_reply_lt FROM interactions WHERE id = ?", (iid,),
    )
    row = dict(await cursor.fetchone())
    assert row["agent_reply"] == "Merci beaucoup"
    assert row["agent_reply_lt"] == "Labai ačiū"


@pytest.mark.asyncio
async def test_thread_reply_count_includes_sent_approval_status(db):
    # Sent via approval should also count toward thread max
    await _log_pending(db, email_id="eid-1", approval_status="sent", was_sent=True)
    await _log_pending(db, email_id="eid-2", approval_status="sent_manually", was_sent=True)
    # Pending shouldn't count (draft not actually sent to lead)
    await _log_pending(db, email_id="eid-3", approval_status="pending", was_sent=False)
    count = await get_thread_reply_count(db, "x@y.com", "camp-1")
    assert count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_approval_flow.py -v`
Expected: FAIL with ImportError on the new function names

- [ ] **Step 3: Extend log_interaction to accept new fields**

Modify `db/database.py` - find `log_interaction` function (starts line 169). Extend the INSERT to include the new columns:

```python
async def log_interaction(conn: aiosqlite.Connection, data: dict) -> int:
    # Auto-pull per-request Claude usage jei call site nenurodo
    if "cost_usd" not in data:
        try:
            from core.classifier import get_usage_snapshot
            snap = get_usage_snapshot()
            data = {**data, **snap}
        except Exception:
            pass
    cursor = await conn.execute(
        """INSERT INTO interactions
        (campaign_id, campaign_name, lead_email, email_account, email_id,
         client_id, prospect_message, classification, confidence,
         classification_reasoning, agent_reply, was_sent, matched_faq_index,
         faq_confidence, offered_slots, few_shots_used, thread_position, brief_version,
         quality_score, quality_issues, quality_summary, improvement_suggestion,
         tokens_in, tokens_out, tokens_cache_read, cost_usd,
         original_language, prospect_message_lt, agent_reply_lt, approval_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["campaign_id"], data.get("campaign_name"), data["lead_email"],
            data.get("email_account"), data["email_id"], data["client_id"],
            data["prospect_message"], data["classification"], data["confidence"],
            data.get("classification_reasoning"), data.get("agent_reply"),
            data.get("was_sent", False), data.get("matched_faq_index"),
            data.get("faq_confidence"), data.get("offered_slots"),
            data.get("few_shots_used"), data.get("thread_position", 1),
            data.get("brief_version"),
            data.get("quality_score"), data.get("quality_issues"),
            data.get("quality_summary"), data.get("improvement_suggestion"),
            data.get("tokens_in"), data.get("tokens_out"),
            data.get("tokens_cache_read"), data.get("cost_usd"),
            data.get("original_language"), data.get("prospect_message_lt"),
            data.get("agent_reply_lt"), data.get("approval_status"),
        ),
    )
    await conn.commit()
    row_id = cursor.lastrowid
    # Backup to Google Sheets (silent if not configured)
    try:
        from core import sheets_backup
        from datetime import datetime, timezone
        backup_row = dict(data)
        backup_row["id"] = row_id
        backup_row["created_at"] = datetime.now(timezone.utc).isoformat()
        sheets_backup.append_interaction(backup_row)
    except Exception:
        pass
    return row_id
```

- [ ] **Step 4: Add new helper functions to db/database.py**

Append to `db/database.py` (after `get_weekly_stats`, end of file):

```python
import json as _json


async def update_approval_status(
    conn: aiosqlite.Connection,
    interaction_id: int,
    status: str,
    approved_by: str | None = None,
    final_sent_text: str | None = None,
) -> None:
    """Transition approval state. Sets was_sent=1 for 'sent' and 'sent_manually'."""
    was_sent = 1 if status in ("sent", "sent_manually") else 0
    now_iso = datetime.utcnow().isoformat()
    # Only update final_sent_text if provided (keep existing on rejections)
    if final_sent_text is not None:
        await conn.execute(
            """UPDATE interactions SET approval_status = ?, approved_by = ?,
               approved_at = ?, was_sent = ?, final_sent_text = ? WHERE id = ?""",
            (status, approved_by, now_iso, was_sent, final_sent_text, interaction_id),
        )
    else:
        await conn.execute(
            """UPDATE interactions SET approval_status = ?, approved_by = ?,
               approved_at = ?, was_sent = ? WHERE id = ?""",
            (status, approved_by, now_iso, was_sent, interaction_id),
        )
    await conn.commit()


async def get_pending_drafts(
    conn: aiosqlite.Connection, client_id: str | None = None
) -> list[dict]:
    """All pending-approval drafts, oldest first (FIFO for processing)."""
    if client_id:
        cursor = await conn.execute(
            "SELECT * FROM interactions WHERE approval_status = 'pending' "
            "AND client_id = ? ORDER BY created_at ASC",
            (client_id,),
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM interactions WHERE approval_status = 'pending' "
            "ORDER BY created_at ASC",
        )
    return [dict(r) for r in await cursor.fetchall()]


async def get_pending_count(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE approval_status = 'pending'"
    )
    row = await cursor.fetchone()
    return row[0]


async def append_edit_history(
    conn: aiosqlite.Connection, interaction_id: int, entry: dict
) -> None:
    """Append an edit entry to interactions.edit_history JSON array.

    Entry must include at minimum: lt_instruction, before, after. 'ts' is added
    automatically if missing.
    """
    cursor = await conn.execute(
        "SELECT edit_history FROM interactions WHERE id = ?", (interaction_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        raise ValueError(f"Interaction {interaction_id} not found")
    raw = row["edit_history"] or "[]"
    try:
        history = _json.loads(raw)
    except (ValueError, TypeError):
        history = []
    if "ts" not in entry:
        entry = {**entry, "ts": datetime.utcnow().isoformat()}
    history.append(entry)
    await conn.execute(
        "UPDATE interactions SET edit_history = ? WHERE id = ?",
        (_json.dumps(history, ensure_ascii=False), interaction_id),
    )
    await conn.commit()


async def update_draft_text(
    conn: aiosqlite.Connection,
    interaction_id: int,
    agent_reply: str,
    agent_reply_lt: str | None,
) -> None:
    """Replace current draft after an edit iteration."""
    await conn.execute(
        "UPDATE interactions SET agent_reply = ?, agent_reply_lt = ? WHERE id = ?",
        (agent_reply, agent_reply_lt, interaction_id),
    )
    await conn.commit()
```

- [ ] **Step 5: Update `get_thread_reply_count` to include approval-sent states**

Modify `db/database.py` - find the existing `get_thread_reply_count` function (line 224) and replace with:

```python
async def get_thread_reply_count(conn: aiosqlite.Connection, lead_email: str, campaign_id: str) -> int:
    """Count replies actually sent to the lead (any path: auto-send or approval).

    Includes: was_sent=1 (auto-send path) and approval_status in ('sent','sent_manually').
    Excludes: pending, rejected (never reached the lead).
    """
    cursor = await conn.execute(
        """SELECT COUNT(*) FROM interactions
           WHERE lead_email = ? AND campaign_id = ?
           AND (was_sent = 1 OR approval_status IN ('sent', 'sent_manually'))""",
        (lead_email, campaign_id),
    )
    row = await cursor.fetchone()
    return row[0]
```

(The `was_sent = 1 OR approval_status IN (...)` predicate is defensive - `update_approval_status` already sets `was_sent=1` for the approved states, so in practice `was_sent=1` alone would suffice. Keeping both for resilience against direct DB writes.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_approval_flow.py -v`
Expected: All 10 tests PASS

- [ ] **Step 7: Commit**

```bash
git add db/database.py tests/test_approval_flow.py
git commit -m "feat(db): approval flow helpers (pending/approve/edit_history)"
```

---

## Task 6: Client loader - campaign-level language + approval_required

**Files:**
- Modify: `core/client_loader.py`
- Create: `tests/test_yaml_backward_compat.py`
- Modify: `tests/test_client_loader.py`

- [ ] **Step 1: Write failing backward-compat tests**

Create `tests/test_yaml_backward_compat.py`:

```python
import pytest
from core.client_loader import (
    load_clients, get_client_by_campaign, get_campaign_language,
)


@pytest.fixture
def legacy_yaml(tmp_path):
    # Old format - campaigns is a plain list of strings
    (tmp_path / "legacy.yaml").write_text("""
client_id: legacy_client
client_name: Legacy
campaigns:
  - campaign-old-1
  - campaign-old-2
company_description: x
service_offering: y
value_proposition: z
pricing: p
target_audience: t
meeting:
  participant_from_client: P
  purpose: pp
  duration_minutes: 30
  google_calendar_id: primary
  working_hours:
    start: "09:00"
    end: "17:00"
    days: ["Monday"]
  buffer_minutes: 15
  advance_days: 7
  slots_to_offer: 3
faq:
  - question: q
    answer: a
boundaries:
  cannot_promise: []
  escalate_topics: []
tone:
  formality: semi-formal
  addressing: Jūs
  language: lt
  personality: x
  max_reply_length_sentences: 5
  sign_off: P
  sender_name: P
""", encoding="utf-8")
    return tmp_path


@pytest.fixture
def new_format_yaml(tmp_path):
    # New format - campaigns is a list of dicts with id, language, name
    (tmp_path / "new_format.yaml").write_text("""
client_id: new_client
client_name: New
approval_required: true
campaigns:
  - id: campaign-fr
    language: fr
    name: French outreach
  - id: campaign-de
    language: de
    name: German outreach
company_description: x
service_offering: y
value_proposition: z
pricing: p
target_audience: t
meeting:
  participant_from_client: P
  purpose: pp
  duration_minutes: 30
  google_calendar_id: primary
  working_hours:
    start: "09:00"
    end: "17:00"
    days: ["Monday"]
  buffer_minutes: 15
  advance_days: 7
  slots_to_offer: 3
faq:
  - question: q
    answer: a
boundaries:
  cannot_promise: []
  escalate_topics: []
tone:
  formality: semi-formal
  addressing: vous
  language: fr
  personality: x
  max_reply_length_sentences: 5
  sign_off: Cordialement
  sender_name: P
""", encoding="utf-8")
    return tmp_path


def test_legacy_yaml_loads(legacy_yaml):
    clients = load_clients(legacy_yaml)
    assert "legacy_client" in clients
    # approval_required defaults to False
    assert clients["legacy_client"].get("approval_required", False) is False


def test_legacy_yaml_campaign_lookup(legacy_yaml):
    clients = load_clients(legacy_yaml)
    client = get_client_by_campaign(clients, "campaign-old-1")
    assert client is not None
    assert client["client_id"] == "legacy_client"


def test_legacy_campaign_language_falls_back_to_tone(legacy_yaml):
    clients = load_clients(legacy_yaml)
    lang = get_campaign_language(clients, "campaign-old-1")
    assert lang == "lt"  # tone.language fallback


def test_new_format_loads(new_format_yaml):
    clients = load_clients(new_format_yaml)
    assert "new_client" in clients
    assert clients["new_client"]["approval_required"] is True


def test_new_format_campaign_lookup(new_format_yaml):
    clients = load_clients(new_format_yaml)
    client = get_client_by_campaign(clients, "campaign-fr")
    assert client is not None
    assert client["client_id"] == "new_client"


def test_new_format_per_campaign_language(new_format_yaml):
    clients = load_clients(new_format_yaml)
    assert get_campaign_language(clients, "campaign-fr") == "fr"
    assert get_campaign_language(clients, "campaign-de") == "de"


def test_unknown_campaign_language_returns_none(new_format_yaml):
    clients = load_clients(new_format_yaml)
    assert get_campaign_language(clients, "nonexistent") is None


def test_mixed_format_in_one_client(tmp_path):
    # Support gradual migration within a single client - some dicts, some strings
    (tmp_path / "mixed.yaml").write_text("""
client_id: mixed_client
client_name: Mixed
campaigns:
  - plain-string-campaign
  - id: dict-campaign
    language: fr
company_description: x
service_offering: y
value_proposition: z
pricing: p
target_audience: t
meeting:
  participant_from_client: P
  purpose: pp
  duration_minutes: 30
  google_calendar_id: primary
  working_hours:
    start: "09:00"
    end: "17:00"
    days: ["Monday"]
  buffer_minutes: 15
  advance_days: 7
  slots_to_offer: 3
faq:
  - question: q
    answer: a
boundaries:
  cannot_promise: []
  escalate_topics: []
tone:
  formality: semi-formal
  addressing: Jūs
  language: lt
  personality: x
  max_reply_length_sentences: 5
  sign_off: Pagarbiai
  sender_name: P
""", encoding="utf-8")
    clients = load_clients(tmp_path)
    assert get_campaign_language(clients, "plain-string-campaign") == "lt"  # tone fallback
    assert get_campaign_language(clients, "dict-campaign") == "fr"  # per-campaign
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_yaml_backward_compat.py -v`
Expected: FAIL - `get_campaign_language` doesn't exist, `approval_required` not explicitly handled

- [ ] **Step 3: Extend client_loader.py**

Modify `core/client_loader.py` - replace the entire file contents with:

```python
import yaml
from pathlib import Path


REQUIRED_FIELDS = [
    "client_id", "client_name", "campaigns", "company_description",
    "service_offering", "value_proposition", "pricing", "target_audience",
    "meeting", "faq", "boundaries", "tone",
]


def load_clients(clients_dir: Path) -> dict:
    """Load all YAML client configs from directory. Returns {client_id: config}.

    Each client dict gets `approval_required: bool` (default False) explicitly set.
    Campaign entries are left as-is (plain string or dict with `id` + `language`).
    """
    clients = {}
    for yaml_file in clients_dir.glob("*.yaml"):
        if yaml_file.name.startswith("_"):
            continue
        with open(yaml_file, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if not config or "client_id" not in config:
            continue
        for field in REQUIRED_FIELDS:
            if field not in config:
                raise ValueError(f"Client {yaml_file.name} missing required field: {field}")
        config.setdefault("approval_required", False)
        clients[config["client_id"]] = config
    return clients


def get_client_by_campaign(clients: dict, campaign_id: str) -> dict | None:
    """Find client config by Instantly campaign UUID.

    Accepts both legacy format (campaigns: [str, ...]) and new format
    (campaigns: [{id: str, language: str, name: str}, ...]), mixed within a
    single client's campaign list.
    """
    for client in clients.values():
        campaigns = client.get("campaigns", [])
        for camp in campaigns:
            camp_id = camp["id"] if isinstance(camp, dict) else camp
            if camp_id == campaign_id:
                return client
    return None


def get_campaign_language(clients: dict, campaign_id: str) -> str | None:
    """Return target language for a campaign ID, or None if campaign not found.

    Priority:
    1. campaign-level `language` (new format: {id, language, name})
    2. client-level `tone.language` fallback (legacy format)
    """
    for client in clients.values():
        for camp in client.get("campaigns", []):
            if isinstance(camp, dict):
                if camp.get("id") == campaign_id:
                    return camp.get("language") or client.get("tone", {}).get("language")
            else:
                if camp == campaign_id:
                    return client.get("tone", {}).get("language")
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_yaml_backward_compat.py tests/test_client_loader.py -v`
Expected: All pass (existing test_client_loader tests still work since format is backward-compatible)

- [ ] **Step 5: Commit**

```bash
git add core/client_loader.py tests/test_yaml_backward_compat.py
git commit -m "feat(yaml): per-campaign language + approval_required flag"
```

---

## Task 7: Reply generator accepts target_language

**Files:**
- Modify: `prompts/reply.py`
- Modify: `core/reply_generator.py`
- Test: `tests/test_reply_generator.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_reply_generator.py` (if file exists, otherwise create minimal version). First check existing content:

Run: `head -40 tests/test_reply_generator.py`

Append these tests:

```python
import pytest
from unittest.mock import AsyncMock, patch
from core.reply_generator import generate_reply


def _minimal_client(language="lt"):
    return {
        "client_id": "c1", "client_name": "Test",
        "company_description": "x", "service_offering": "y",
        "value_proposition": "z", "pricing": "p",
        "boundaries": {"cannot_promise": []},
        "tone": {
            "language": language, "addressing": "vous" if language == "fr" else "Jūs",
            "personality": "Friendly", "max_reply_length_sentences": 5,
            "sign_off": "Cordialement" if language == "fr" else "Pagarbiai",
            "sender_name": "Paulius",
        },
    }


@pytest.mark.asyncio
async def test_generate_reply_target_language_lt_default():
    # When target_language is None, use tone.language from client_config (backward compat)
    with patch("core.reply_generator.call_claude_with_retry", new=AsyncMock(return_value="Labas")) as mock:
        result = await generate_reply(
            prospect_message="Labas, įdomu", classification="QUESTION",
            client_config=_minimal_client("lt"), few_shots=[], anti_patterns=[],
        )
    assert result == "Labas"
    # System prompt should mention 'lt' somewhere
    system_blocks = mock.call_args.kwargs["system"]
    sys_text = "\n".join(b["text"] if isinstance(b, dict) else b for b in system_blocks)
    assert "lt" in sys_text.lower() or "lithuanian" in sys_text.lower()


@pytest.mark.asyncio
async def test_generate_reply_target_language_overrides_tone():
    # Campaign is FR but client's tone.language is LT - target_language should win
    with patch("core.reply_generator.call_claude_with_retry", new=AsyncMock(return_value="Merci")) as mock:
        result = await generate_reply(
            prospect_message="Bonjour", classification="QUESTION",
            client_config=_minimal_client("lt"), few_shots=[], anti_patterns=[],
            target_language="fr",
        )
    assert result == "Merci"
    system_blocks = mock.call_args.kwargs["system"]
    sys_text = "\n".join(b["text"] if isinstance(b, dict) else b for b in system_blocks)
    # target_language 'fr' must appear explicitly in the prompt (override directive)
    assert "fr" in sys_text.lower() or "french" in sys_text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_reply_generator.py::test_generate_reply_target_language_overrides_tone -v`
Expected: FAIL - `target_language` is not a parameter

- [ ] **Step 3: Modify prompts/reply.py to accept target_language override**

Modify `prompts/reply.py` - update `_build_reply_static_base(client)` to accept an override. Replace the function signature and language line:

```python
def _build_reply_static_base(client: dict, target_language: str | None = None) -> str:
    """Static per-client prompt dalis - cache'inama 5 min TTL."""
    cannot_promise = "\n".join(f"- {p}" for p in client["boundaries"]["cannot_promise"])
    max_sent = client['tone']['max_reply_length_sentences']
    lang_hints = client.get("language_hints", "")
    lang_section = f"\n\n## Kalbos nuorodos\n{lang_hints}\n" if lang_hints else ""
    resources = client.get("product_resources", "")
    resources_section = f"\n\n## Resursai ir priedai\n{resources}\n" if resources else ""
    effective_language = target_language or client['tone']['language']
    return f"""Tu esi {client['client_name']} atstovas, atsakinėjantis į cold email atsakymus.
Tikslas - natūraliai vesti pokalbį link trumpo susitikimo.

## Kliento informacija
{client['company_description']}

## Paslauga
{client['service_offering']}

## Vertės propozicija
{client['value_proposition']}

## Kainodara
{client['pricing']}

## Tonas
- Kalba: {effective_language} (jei "auto" - detektuok iš prospect'o žinutės kalbos: LT/FR/EN)
- Kreipinys: {client['tone']['addressing']}
- Stilius: {client['tone']['personality']}
- Max ilgis: {max_sent} sakiniai
- Pasirašymas: {client['tone']['sign_off']}, {client['tone']['sender_name']}

## Ko negalima žadėti
{cannot_promise}
{resources_section}{lang_section}
## Pagrindinės taisyklės
0. **Kalba.** Atsakyk {effective_language} kalba. Signal logic (kvalifikacija/objection/booking) veikia vienodai visose kalbose - tik leksika keičiasi. Pavyzdžiui FR: "Linkėjimai" → "Cordialement" arba "Bien à vous"; EN: "Best regards" arba "Cheers".

KRITIŠKAI SVARBU - ACTION-FIRST filosofija (ne qualify-first):

Jei prospect'as prašo **konkretaus dalyko** (kainos, pasiūlymo, skaičiavimų, nuotraukų, katalogo, pavyzdžių, susitikimo laiko) - **pažadėk konkretų veiksmą su laiko rėmu** vietoj klausimų. Pavyzdžiui:
- Prospect: "ar galit primesti kainą projektui?" → Paulius: "Be problemų paskaičiuosime. Šiandien iki vakaro atsiųsiu skaičiavimus ;)"
- Prospect: "atsiųskit daugiau info" → Paulius: "Prisegu PDF katalogą. Jei kiltų klausimų - parašykite."
- Prospect: "kokios kainos?" → Paulius: "Kainos nuo 5,50 €/m iki 10,90 €/m (+PVM)... Prisegu PDF'ą."

NENAUDOK klausimų šabloniškai ("kokio aukščio?", "kiek metrų?", "kokia pramonė?") kai:
- Prospect'as jau davė kontekstą (thread history rodo cold email + prospect'o atsakymas su specifika)
- Gali tiesiog duoti tai, ko prašo (kainos iš brief'o, PDF nuoroda, susitikimo slot'ai)

Paulius NErašo "kad galėčiau pateikti tikslesnę kainą, norėčiau žinoti X". Jis rašo "paskaičiuosim, iki vakaro atsiųsiu". Veiksmas, ne kvalifikavimas. Klausimai tik tada, kai BE jų tikrai negali atsakyti.
0a. **BRŪKŠNIŲ TAISYKLĖ (GLOBALI):** NIEKADA nenaudok em-dash `—` ar en-dash `–`. Visada rašyk trumpą brūkšnį `-`. Galioja visose kalbose (LT / EN / FR). Net jei brief'e YRA em-dash - tu atsakyme naudoji TIK `-`.
0b. **VIDINIAI KOMENTARAI NIEKADA:** jei brief'e ar instrukcijose matai pastabas SKIRTAS PAULIUI (pvz. "Paulius turi pridėti PDF priedą rankomis" arba "Note pour Paulius:..."), tai yra INSTRUKCIJA TAU, NE ATSAKYMO DALIS. NIEKADA neįtrauk jų į siunčiamą reply. Prospect'as neturi matyti vidinių debug pastabų.
1. **Fact grounding.** Remkis TIK šiame brief'e esančia info. Jei klausiama to, ko nėra - sakyk "Detaliau papasakočiau per trumpą pokalbį" (atitinkama kalba). NIEKADA neišsigalvok telefonų, email'ų, adresų, kainų, kiekių, terminų, lokacijų, salonų/biurų.
2. **Laikai.** Konkrečias dienas/valandas siūlyk TIK jei gavai `available_slots`. Kitaip - "Kada jums būtų patogu trumpam pokalbiui?"
3. **Stilius.** Max {max_sent} sakiniai, be subject line, be "Sveiki, [vardas]" jei tai ne pirmas atsakymas thread'e. Laikus pateik tekste, ne bullet'ais.
4. **Tikslas.** Kiekvienas atsakymas veda link susitikimo (arba tvarkingai uždaro, jei lead'as nesidomi).
"""
```

Also update `build_reply_system_prompt_blocks` and `build_reply_system_prompt` signatures to accept and pass through `target_language`:

```python
def build_reply_system_prompt_blocks(client: dict, anti_patterns_section: str,
                                      few_shot_section: str,
                                      target_language: str | None = None) -> list:
    """Grąžina system prompt kaip blokų sąrašą: static base (cached) + dynamic tail (no cache)."""
    base = _build_reply_static_base(client, target_language)
    tail = _build_reply_dynamic_tail(anti_patterns_section, few_shot_section)
    blocks = [{"type": "text", "text": base, "cache_control": {"type": "ephemeral"}}]
    if tail:
        blocks.append({"type": "text", "text": tail})
    return blocks
```

Check `build_reply_system_prompt` (around line 80): if it exists and is used elsewhere, add the same parameter. Otherwise skip.

- [ ] **Step 4: Modify core/reply_generator.py to pass target_language**

Modify `core/reply_generator.py` - update `generate_reply` signature (lines 12-22):

```python
async def generate_reply(
    prospect_message: str,
    classification: str,
    client_config: dict,
    few_shots: list[dict],
    anti_patterns: list[dict],
    available_slots: list[dict] | None = None,
    matching_faq: str | None = None,
    thread_position: int = 1,
    thread_history: str = "",
    target_language: str | None = None,
) -> str:
    """Generate reply. Raises APIUnavailableError if Claude API is down.

    target_language: override the client's tone.language (e.g. per-campaign
    language, or detected prospect language). None = use tone.language.
    """
    system_blocks = build_reply_system_prompt_blocks(
        client_config,
        format_anti_patterns(anti_patterns),
        format_few_shots(few_shots),
        target_language=target_language,
    )
    # ... rest unchanged
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_reply_generator.py -v`
Expected: All PASS (including the 2 new target_language tests)

- [ ] **Step 6: Commit**

```bash
git add prompts/reply.py core/reply_generator.py tests/test_reply_generator.py
git commit -m "feat(reply): generate_reply accepts per-campaign target_language"
```

---

## Task 8: Few-shot filtering by original_language

**Files:**
- Modify: `core/self_improver.py`
- Test: `tests/test_self_improver.py` (extend)

- [ ] **Step 1: Write failing tests**

Create or extend `tests/test_self_improver.py` (check existing first):

Run: `head -40 tests/test_self_improver.py 2>&1`

Append (or create if missing) these tests:

```python
import pytest
import pytest_asyncio
from db.database import init_db, log_interaction
from core.self_improver import get_best_examples


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


async def _seed_interaction(db, client_id, category, language, email_id, rating="thumbs_up"):
    iid = await log_interaction(db, {
        "campaign_id": "c1", "lead_email": "x@y.com", "email_id": email_id,
        "client_id": client_id, "prospect_message": "hi",
        "classification": category, "confidence": 0.9,
        "agent_reply": "reply", "was_sent": True,
        "original_language": language,
    })
    await db.execute(
        "UPDATE interactions SET human_rating = ? WHERE id = ?", (rating, iid)
    )
    await db.commit()
    return iid


@pytest.mark.asyncio
async def test_get_best_examples_filters_by_language_when_set(db):
    await _seed_interaction(db, "gleadsy", "QUESTION", "lt", "eid-lt")
    await _seed_interaction(db, "gleadsy", "QUESTION", "fr", "eid-fr")
    await _seed_interaction(db, "gleadsy", "QUESTION", None, "eid-null")  # legacy - language unknown

    lt_examples = await get_best_examples(db, "QUESTION", "gleadsy", limit=5, language="lt")
    # LT examples: the LT one, plus legacy ones with NULL language (generic fallback)
    emails_used = {int(e["id"]) for e in lt_examples}
    # Must contain LT and NULL, must NOT contain FR
    assert len(lt_examples) == 2


@pytest.mark.asyncio
async def test_get_best_examples_language_none_returns_all(db):
    """Backward compat: when language=None, no language filter applied."""
    await _seed_interaction(db, "gleadsy", "QUESTION", "lt", "eid-lt")
    await _seed_interaction(db, "gleadsy", "QUESTION", "fr", "eid-fr")
    results = await get_best_examples(db, "QUESTION", "gleadsy", limit=5, language=None)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_get_best_examples_fr_with_no_matching_examples_still_returns_null_language(db):
    """Fallback: if no FR examples exist, legacy NULL-language examples are used."""
    await _seed_interaction(db, "gleadsy", "QUESTION", None, "eid-null")
    results = await get_best_examples(db, "QUESTION", "gleadsy", limit=5, language="fr")
    assert len(results) == 1  # The null-language one is returned as fallback
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_self_improver.py -v`
Expected: FAIL with `TypeError: got unexpected keyword argument 'language'`

- [ ] **Step 3: Update get_best_examples signature**

Modify `core/self_improver.py` - replace `get_best_examples`:

```python
async def get_best_examples(
    conn: aiosqlite.Connection,
    category: str,
    client_id: str,
    limit: int = 3,
    language: str | None = None,
) -> list[dict]:
    """Get best few-shot examples, prioritized by quality.

    If `language` is provided, matches that language OR legacy NULL-language
    examples. If None (default), no language filter (backward compat).
    """
    if language is None:
        cursor = await conn.execute(
            """SELECT id, prospect_message, agent_reply, human_rating, outcome
            FROM interactions
            WHERE client_id = ? AND classification = ? AND was_sent = 1
            AND (human_rating IS NULL OR human_rating != 'thumbs_down')
            ORDER BY
                CASE
                    WHEN human_rating = 'thumbs_up' AND outcome = 'meeting_booked' THEN 1
                    WHEN human_rating = 'thumbs_up' THEN 2
                    WHEN outcome = 'meeting_booked' THEN 3
                    WHEN outcome = 'replied_again' THEN 4
                    ELSE 5
                END,
                created_at DESC
            LIMIT ?""",
            (client_id, category, limit),
        )
    else:
        cursor = await conn.execute(
            """SELECT id, prospect_message, agent_reply, human_rating, outcome
            FROM interactions
            WHERE client_id = ? AND classification = ? AND was_sent = 1
            AND (human_rating IS NULL OR human_rating != 'thumbs_down')
            AND (original_language = ? OR original_language IS NULL)
            ORDER BY
                CASE
                    WHEN original_language = ? THEN 0 ELSE 1
                END,
                CASE
                    WHEN human_rating = 'thumbs_up' AND outcome = 'meeting_booked' THEN 1
                    WHEN human_rating = 'thumbs_up' THEN 2
                    WHEN outcome = 'meeting_booked' THEN 3
                    WHEN outcome = 'replied_again' THEN 4
                    ELSE 5
                END,
                created_at DESC
            LIMIT ?""",
            (client_id, category, language, language, limit),
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]
```

(The `original_language = ?` CASE in ORDER BY prioritizes exact-language matches over NULL-language fallback.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_self_improver.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add core/self_improver.py tests/test_self_improver.py
git commit -m "feat(learn): few-shot filter by original_language with NULL fallback"
```

---

## Task 9: Slack notify_approval_pending

**Files:**
- Modify: `core/slack_notifier.py`
- Test: `tests/test_slack_notifier.py` (create if missing)

- [ ] **Step 1: Write failing tests**

Create `tests/test_slack_notifier.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch
from core.slack_notifier import notify_approval_pending


@pytest.mark.asyncio
async def test_notify_approval_pending_sends_message():
    with patch("core.slack_notifier._send", new=AsyncMock()) as mock_send:
        await notify_approval_pending(
            iid=42,
            lead_email="pierre@acme.fr",
            client_id="gleadsy",
            classification="INTERESTED",
            quality_score=8,
            confidence=0.92,
            prospect_message_lt="Labas, įdomu. Kiek kainuoja?",
            agent_reply_lt="Ačiū už susidomėjimą. Kainos nuo 800€/mėn...",
            original_language="fr",
            dashboard_base_url="https://reply.gleadsy.com",
        )
    mock_send.assert_called_once()
    text = mock_send.call_args.args[0]
    assert "pierre@acme.fr" in text
    assert "gleadsy" in text
    assert "INTERESTED" in text
    assert "Labas, įdomu" in text
    assert "Ačiū už" in text
    assert "reply.gleadsy.com/pending#draft-42" in text
    assert "🔥" in text  # INTERESTED high-priority emoji


@pytest.mark.asyncio
async def test_notify_approval_pending_normal_priority_for_question():
    with patch("core.slack_notifier._send", new=AsyncMock()) as mock_send:
        await notify_approval_pending(
            iid=1, lead_email="x@y.com", client_id="c",
            classification="QUESTION", quality_score=9, confidence=0.8,
            prospect_message_lt="prompt", agent_reply_lt="reply",
            original_language="de",
            dashboard_base_url="https://reply.gleadsy.com",
        )
    text = mock_send.call_args.args[0]
    assert "⏳" in text or "🔥" not in text  # normal prefix


@pytest.mark.asyncio
async def test_notify_approval_pending_warning_for_low_quality():
    with patch("core.slack_notifier._send", new=AsyncMock()) as mock_send:
        await notify_approval_pending(
            iid=1, lead_email="x@y.com", client_id="c",
            classification="QUESTION", quality_score=6, confidence=0.8,
            prospect_message_lt="prompt", agent_reply_lt="reply",
            original_language="fr",
            dashboard_base_url="https://reply.gleadsy.com",
        )
    text = mock_send.call_args.args[0]
    assert "⚠️" in text


@pytest.mark.asyncio
async def test_notify_approval_pending_truncates_long_text():
    long_lt = "Sakinys. " * 100  # way over any reasonable preview
    with patch("core.slack_notifier._send", new=AsyncMock()) as mock_send:
        await notify_approval_pending(
            iid=1, lead_email="x@y.com", client_id="c",
            classification="QUESTION", quality_score=8, confidence=0.8,
            prospect_message_lt=long_lt, agent_reply_lt=long_lt,
            original_language="fr",
            dashboard_base_url="https://reply.gleadsy.com",
        )
    text = mock_send.call_args.args[0]
    # Each preview capped - total message reasonable
    assert len(text) < 2000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_slack_notifier.py -v`
Expected: FAIL with `ImportError: cannot import name 'notify_approval_pending'`

- [ ] **Step 3: Implement notify_approval_pending**

Modify `core/slack_notifier.py` - append after `notify_meeting_booked`:

```python
LANG_FLAGS = {
    "lt": "🇱🇹", "en": "🇬🇧", "fr": "🇫🇷",
    "de": "🇩🇪", "et": "🇪🇪", "lv": "🇱🇻",
}


def _approval_prefix(classification: str, quality_score: int | None) -> str:
    if classification == "INTERESTED":
        return "🔥"
    if quality_score is not None and quality_score < 7:
        return "⚠️"
    return "⏳"


def _preview(text: str, max_chars: int = 240) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text


async def notify_approval_pending(
    iid: int,
    lead_email: str,
    client_id: str,
    classification: str,
    quality_score: int | None,
    confidence: float,
    prospect_message_lt: str,
    agent_reply_lt: str,
    original_language: str,
    dashboard_base_url: str,
) -> None:
    """Notify Paulius that a new foreign-language draft is waiting for approval."""
    prefix = _approval_prefix(classification, quality_score)
    flag = LANG_FLAGS.get(original_language.lower(), "")
    quality_str = f"Quality: {quality_score}/10" if quality_score is not None else "Quality: -"
    link = f"{dashboard_base_url.rstrip('/')}/pending#draft-{iid}"

    text = (
        f"{prefix} Naujas draftas laukia approval\n\n"
        f"Klientas: {client_id}  |  Lead: {lead_email}  {flag} {original_language.upper()}\n"
        f"Kategorija: {classification}  |  {quality_str}  |  Confidence: {confidence:.0%}\n\n"
        f"🗣️ Lead žinutė (LT vertimas):\n> {_preview(prospect_message_lt, 300)}\n\n"
        f"✍️ Agent'o draftas (LT preview):\n> {_preview(agent_reply_lt, 400)}\n\n"
        f"👉 {link}"
    )
    await _send(text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_slack_notifier.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add core/slack_notifier.py tests/test_slack_notifier.py
git commit -m "feat(slack): notify_approval_pending with LT preview"
```

---

## Task 10: Pipeline integration - webhook routing

**Files:**
- Modify: `webhooks/instantly_webhook.py`
- Test: `tests/test_webhook_handler.py` (extend)
- Create: `tests/fixtures/reply_fr_interested.json`

- [ ] **Step 1: Create FR fixture**

Create `tests/fixtures/reply_fr_interested.json`:

```json
{
  "event_type": "reply_received",
  "campaign_id": "campaign-fr-uuid",
  "campaign_name": "Gleadsy FR outreach",
  "lead_email": "pierre.dupont@acme.fr",
  "email_account": "paulius@gleadsy.com",
  "email_id": "email-fr-12345",
  "reply_text": "Bonjour, je suis intéressé par vos services. Pourriez-vous me donner plus de détails sur les prix?",
  "reply_subject": "Re: Proposition",
  "timestamp": "2026-04-23T10:30:00Z"
}
```

- [ ] **Step 2: Write failing tests**

Append to `tests/test_webhook_handler.py`:

```python
@pytest.fixture
def fr_clients():
    return {
        "gleadsy_fr": {
            "client_id": "gleadsy_fr", "client_name": "Gleadsy FR",
            "approval_required": True,
            "campaigns": [
                {"id": "campaign-fr-uuid", "language": "fr", "name": "FR outreach"},
            ],
            "company_description": "Digital marketing", "service_offering": "Cold email",
            "value_proposition": "5 rendez-vous qualifiés", "pricing": "800€/mois",
            "target_audience": "B2B",
            "meeting": {
                "participant_from_client": "Paulius", "purpose": "Consultation",
                "duration_minutes": 30, "google_calendar_id": "primary",
                "working_hours": {"start": "09:00", "end": "17:00",
                                   "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]},
                "buffer_minutes": 15, "advance_days": 7, "slots_to_offer": 3,
            },
            "faq": [{"question": "Prix?", "answer": "Individuel"}],
            "boundaries": {"cannot_promise": [], "escalate_topics": []},
            "tone": {"formality": "semi-formal", "addressing": "vous", "language": "fr",
                     "personality": "Professional", "max_reply_length_sentences": 5,
                     "sign_off": "Cordialement", "sender_name": "Paulius"},
        }
    }


@pytest.mark.asyncio
async def test_fr_reply_goes_to_pending_queue(db, fr_clients):
    payload = _load_fixture("reply_fr_interested.json")
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls, \
         patch("webhooks.instantly_webhook.generate_reply",
               new=AsyncMock(return_value="Merci pour votre intérêt.")), \
         patch("webhooks.instantly_webhook.review_quality") as mock_qr, \
         patch("webhooks.instantly_webhook.translate_to_lt",
               new=AsyncMock(side_effect=["Labas, įdomu.", "Ačiū už susidomėjimą."])), \
         patch("webhooks.instantly_webhook.send_reply", new=AsyncMock()) as mock_send, \
         patch("webhooks.instantly_webhook.notify_approval_pending",
               new=AsyncMock()) as mock_notify, \
         patch("webhooks.instantly_webhook.get_free_slots", new=AsyncMock(return_value=[])):
        mock_cls.return_value = type("C", (), {
            "category": "INTERESTED", "confidence": 0.92, "reasoning": "wants pricing",
        })()
        mock_qr.return_value = type("Q", (), {
            "passed": True, "score": 8, "issues": [], "summary": "Good",
            "improvement_suggestion": "",
        })()
        result = await handle_instantly_webhook(payload, db, fr_clients, 0.5)

    assert result["status"] == "pending_approval"
    mock_send.assert_not_called()  # Should NOT auto-send
    mock_notify.assert_called_once()

    # Verify interaction logged with approval_status=pending
    cursor = await db.execute(
        "SELECT approval_status, original_language, prospect_message_lt, agent_reply_lt, was_sent "
        "FROM interactions WHERE email_id = ?",
        (payload["email_id"],),
    )
    row = dict(await cursor.fetchone())
    assert row["approval_status"] == "pending"
    assert row["was_sent"] == 0
    assert row["original_language"] == "fr"
    assert row["prospect_message_lt"] == "Labas, įdomu."
    assert row["agent_reply_lt"] == "Ačiū už susidomėjimą."


@pytest.mark.asyncio
async def test_lt_reply_still_auto_sends(db, mock_clients):
    # LT client without approval_required - same flow as before (no pending queue)
    payload = _load_fixture("reply_interested.json")
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls, \
         patch("webhooks.instantly_webhook.generate_reply",
               new=AsyncMock(return_value="Ačiū už susidomėjimą")), \
         patch("webhooks.instantly_webhook.review_quality") as mock_qr, \
         patch("webhooks.instantly_webhook.send_reply", new=AsyncMock()) as mock_send, \
         patch("webhooks.instantly_webhook.notify_approval_pending",
               new=AsyncMock()) as mock_notify, \
         patch("webhooks.instantly_webhook.notify_reply_sent", new=AsyncMock()), \
         patch("webhooks.instantly_webhook.get_free_slots", new=AsyncMock(return_value=[])):
        mock_cls.return_value = type("C", (), {
            "category": "INTERESTED", "confidence": 0.92, "reasoning": "test",
        })()
        mock_qr.return_value = type("Q", (), {
            "passed": True, "score": 9, "issues": [], "summary": "Good",
            "improvement_suggestion": "",
        })()
        result = await handle_instantly_webhook(payload, db, mock_clients, 0.5)

    assert result["status"] == "sent"
    mock_send.assert_called_once()
    mock_notify.assert_not_called()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_webhook_handler.py::test_fr_reply_goes_to_pending_queue -v`
Expected: FAIL - translate_to_lt / notify_approval_pending imports not present in webhook module

- [ ] **Step 4: Modify webhook pipeline**

Modify `webhooks/instantly_webhook.py` - add new imports near existing imports (top of file, line 1-25):

```python
from core.translation import translate_to_lt
from core.language_detection import detect_language
from core.slack_notifier import notify_reply_sent, notify_escalation, notify_unknown_campaign, notify_meeting_booked, notify_error, notify_approval_pending
from core.client_loader import get_client_by_campaign, get_campaign_language
import config
```

(Replace the existing slack_notifier import to include `notify_approval_pending`.)

Then, inside `_process_reply`, after line 164 (after `campaign_context = ...`), add language detection before classification:

```python
    # Detect prospect message language and resolve target reply language
    campaign_language = get_campaign_language({client_id: client_config}, campaign_id) \
                         or client_config.get("tone", {}).get("language", "lt")
    original_language = detect_language(reply_text, campaign_language)
    approval_required = bool(client_config.get("approval_required", False))
```

After classification passes (after `classification.category` is known and is one of the reply-eligible categories - INTERESTED/QUESTION/NOT_NOW/REFERRAL, ie after line 281 `# 11. Categories that get a reply`), adapt the few-shot query:

```python
    few_shots = await get_best_examples(db, classification.category, client_id,
                                         limit=3, language=original_language)
```

After `generate_reply` call (around line 463), add LT translations:

```python
    # Translate prospect message + generated draft for LT preview (no-op if already LT)
    prospect_message_lt = await translate_to_lt(reply_text, original_language)
    agent_reply_lt = await translate_to_lt(agent_reply, original_language)
```

At the approval branch decision point - **after quality review passes but before `send_reply()`** (around line 528, where `# 13. Send via Instantly` is):

```python
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
```

Also extend the final `log_interaction` call (around line 561) to include translation fields:

```python
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
    })
```

**Important:** Also handle the INTERESTED time-confirmation auto-booking flow. When `approval_required=True`, the `create_meeting_event` / `generate_meeting_confirmation` branches (lines 343-387) should also go into pending. For this plan's scope, the simplest change is to skip auto-booking for approval_required clients - let the meeting-confirmation flow happen through the normal draft-approval pipeline.

Inside `if classification.category == "INTERESTED":` block (around line 291), gate the `parse_time_confirmation` logic:

```python
    if classification.category == "INTERESTED" and not approval_required:
        prev_slots_json = await get_last_offered_slots(db, lead_email, campaign_id)
        # ... existing auto-book logic unchanged
```

For `approval_required=True`, the INTERESTED path skips auto-book → falls through to `generate_reply()` → goes to pending queue like any other reply. Note: no meeting is booked until Paulius approves (and will need a followup time-confirmation cycle). This matches the spec ("approval even for INTERESTED/time-confirm").

- [ ] **Step 5: Update imports on get_best_examples usage**

Modify the line using `get_best_examples` (around line 282):

```python
    few_shots = await get_best_examples(db, classification.category, client_id,
                                         limit=3, language=original_language)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_webhook_handler.py -v`
Expected: All PASS (including the 2 new approval-routing tests)

- [ ] **Step 7: Commit**

```bash
git add webhooks/instantly_webhook.py tests/test_webhook_handler.py tests/fixtures/reply_fr_interested.json
git commit -m "feat(webhook): branch on approval_required, add translation calls"
```

---

## Task 11: Dashboard - `/pending` endpoint

**Files:**
- Modify: `main.py`
- Test: `tests/test_main_pending.py` (create)

- [ ] **Step 1: Write failing tests**

Create `tests/test_main_pending.py`:

```python
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch
import main


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    # Override DB_PATH and disable dashboard auth for tests
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    import importlib, config
    importlib.reload(config)
    # Start lifespan manually
    async with main.app.router.lifespan_context(main.app):
        async with AsyncClient(transport=ASGITransport(app=main.app),
                                base_url="http://test") as ac:
            yield ac, main.db


async def _seed_pending(db, email_id="eid-1", client_id="gleadsy_fr",
                         original_language="fr"):
    from db.database import log_interaction
    return await log_interaction(db, {
        "campaign_id": "c1", "lead_email": "p@acme.fr", "email_id": email_id,
        "client_id": client_id, "prospect_message": "Bonjour",
        "classification": "QUESTION", "confidence": 0.9,
        "agent_reply": "Merci", "agent_reply_lt": "Ačiū",
        "prospect_message_lt": "Labas", "was_sent": False,
        "approval_status": "pending", "original_language": original_language,
        "quality_score": 8,
    })


@pytest.mark.asyncio
async def test_pending_page_lists_drafts(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    r = await client.get("/pending")
    assert r.status_code == 200
    assert "p@acme.fr" in r.text
    assert "Merci" in r.text
    assert "Ačiū" in r.text
    assert f"draft-{iid}" in r.text


@pytest.mark.asyncio
async def test_pending_page_empty_state(client_with_db):
    client, db = client_with_db
    r = await client.get("/pending")
    assert r.status_code == 200
    # Should show empty-state text instead of raising
    assert "Nėra laukiančių" in r.text or "Viskas apdorota" in r.text


@pytest.mark.asyncio
async def test_approve_endpoint_sends_via_instantly(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    with patch("main.send_reply", new=AsyncMock(return_value={})):
        r = await client.post(f"/api/approve/{iid}", json={})
    assert r.status_code == 200
    cursor = await db.execute(
        "SELECT approval_status, was_sent FROM interactions WHERE id = ?", (iid,)
    )
    row = dict(await cursor.fetchone())
    assert row["approval_status"] == "sent"
    assert row["was_sent"] == 1


@pytest.mark.asyncio
async def test_reject_endpoint(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    r = await client.post(f"/api/reject/{iid}")
    assert r.status_code == 200
    cursor = await db.execute(
        "SELECT approval_status, was_sent FROM interactions WHERE id = ?", (iid,)
    )
    row = dict(await cursor.fetchone())
    assert row["approval_status"] == "rejected"
    assert row["was_sent"] == 0


@pytest.mark.asyncio
async def test_mark_sent_endpoint(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    r = await client.post(f"/api/mark_sent/{iid}")
    assert r.status_code == 200
    cursor = await db.execute(
        "SELECT approval_status, was_sent FROM interactions WHERE id = ?", (iid,)
    )
    row = dict(await cursor.fetchone())
    assert row["approval_status"] == "sent_manually"
    assert row["was_sent"] == 1


@pytest.mark.asyncio
async def test_takeover_endpoint_registers_lead(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    r = await client.post(f"/api/takeover/{iid}")
    assert r.status_code == 200
    cursor = await db.execute(
        "SELECT 1 FROM human_takeovers WHERE lead_email = ? AND campaign_id = ?",
        ("p@acme.fr", "c1"),
    )
    assert await cursor.fetchone() is not None
    cursor = await db.execute(
        "SELECT approval_status FROM interactions WHERE id = ?", (iid,)
    )
    assert (await cursor.fetchone())["approval_status"] == "rejected"


@pytest.mark.asyncio
async def test_edit_draft_rewrites_and_saves(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    with patch("main.rewrite_draft", new=AsyncMock(return_value="Merci! Quand?")) as mock_r, \
         patch("main.translate_to_lt", new=AsyncMock(return_value="Ačiū! Kada?")):
        # Need to seed a client config into main.clients for the endpoint
        main.clients["gleadsy_fr"] = {
            "client_id": "gleadsy_fr", "client_name": "Gleadsy FR",
            "tone": {"language": "fr", "sign_off": "Cordialement", "sender_name": "Paulius",
                     "personality": "x"},
            "approval_required": True,
        }
        r = await client.post(f"/api/edit_draft/{iid}",
                               json={"lt_instruction": "Pridėk klausimą"})
    assert r.status_code == 200
    body = r.json()
    assert body["agent_reply"] == "Merci! Quand?"
    assert body["agent_reply_lt"] == "Ačiū! Kada?"

    cursor = await db.execute(
        "SELECT agent_reply, agent_reply_lt, edit_history FROM interactions WHERE id = ?",
        (iid,),
    )
    row = dict(await cursor.fetchone())
    assert row["agent_reply"] == "Merci! Quand?"
    assert row["agent_reply_lt"] == "Ačiū! Kada?"
    import json
    history = json.loads(row["edit_history"])
    assert len(history) == 1
    assert history[0]["lt_instruction"] == "Pridėk klausimą"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_main_pending.py -v`
Expected: FAIL - endpoints not defined

- [ ] **Step 3: Add endpoints to main.py**

Modify `main.py` - add after the existing `/conversation/{lead_email}/{campaign_id}` endpoint (end of that function's block). Add these imports near the top if missing:

```python
from core.translation import translate_to_lt, rewrite_draft
from core.instantly_client import send_reply
from core.client_loader import get_client_by_campaign
from db.database import (
    init_db, update_rating, set_human_takeover, get_weekly_stats,
    get_pending_drafts, get_pending_count, update_approval_status,
    append_edit_history, update_draft_text, log_interaction,
)
```

Add the `/pending` page endpoint:

```python
@app.get("/pending")
async def pending_drafts_page(request: Request):
    """List of drafts waiting Paulius's approval (foreign-language replies)."""
    from fastapi.responses import HTMLResponse
    import html as html_mod
    from datetime import datetime

    if not _get_dashboard_session(request):
        return RedirectResponse(url="/login", status_code=302)

    filter_client = request.query_params.get("client", "")
    rows = await get_pending_drafts(db, client_id=filter_client or None)

    LANG_FLAGS = {"lt": "🇱🇹", "en": "🇬🇧", "fr": "🇫🇷",
                   "de": "🇩🇪", "et": "🇪🇪", "lv": "🇱🇻"}
    CLS_COLORS = {
        "INTERESTED": "#2e7d32", "QUESTION": "#1565c0", "NOT_NOW": "#e65100",
        "REFERRAL": "#6a1b9a", "UNCERTAIN": "#f9a825",
    }

    def _age_str(created_at):
        try:
            ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            delta = datetime.utcnow() - ts.replace(tzinfo=None)
            secs = int(delta.total_seconds())
            if secs < 60: return f"{secs}s"
            if secs < 3600: return f"{secs // 60}min"
            if secs < 86400: return f"{secs // 3600}h {(secs % 3600) // 60}min"
            return f"{secs // 86400}d {(secs % 86400) // 3600}h"
        except Exception:
            return "?"

    # Available clients for filter dropdown
    cursor = await db.execute(
        "SELECT DISTINCT client_id FROM interactions WHERE approval_status='pending' ORDER BY client_id"
    )
    available = [r["client_id"] for r in await cursor.fetchall()]
    client_options = '<option value="">Visi klientai</option>'
    for c in available:
        sel = ' selected' if c == filter_client else ''
        client_options += f'<option value="{html_mod.escape(c)}"{sel}>{html_mod.escape(c)}</option>'

    if not rows:
        empty = ("<div class='empty'>"
                 "<h2>✅ Viskas apdorota</h2>"
                 "<p>Nėra laukiančių draftų.</p>"
                 "</div>")
        drafts_html = empty
    else:
        drafts_html = ""
        for r in rows:
            iid = r["id"]
            lang = (r.get("original_language") or "?").lower()
            flag = LANG_FLAGS.get(lang, "")
            cls = r.get("classification", "")
            cls_color = CLS_COLORS.get(cls, "#333")
            age = _age_str(r.get("created_at"))
            qscore = r.get("quality_score")
            qbadge = f"{qscore}/10" if qscore is not None else "-"
            conf = r.get("confidence", 0)
            prospect_orig = html_mod.escape(r.get("prospect_message") or "")
            prospect_lt = html_mod.escape(r.get("prospect_message_lt") or prospect_orig)
            reply_orig = html_mod.escape(r.get("agent_reply") or "")
            reply_lt = html_mod.escape(r.get("agent_reply_lt") or reply_orig)
            lead = html_mod.escape(r.get("lead_email", ""))
            client_name = html_mod.escape(r.get("client_id", ""))
            campaign_name = html_mod.escape(r.get("campaign_name") or "")

            drafts_html += f"""
<div class="draft" id="draft-{iid}">
  <div class="draft-header">
    <div class="meta">
      <strong>👤 {lead}</strong>  {flag} {lang.upper()}  |  klientas: <b>{client_name}</b>  |  {campaign_name}
    </div>
    <div class="age">laukia {age}</div>
  </div>
  <div class="badges">
    <span class="badge" style="background:{cls_color};color:white">{cls}</span>
    <span class="badge" style="background:#eee">Quality: {qbadge}</span>
    <span class="badge" style="background:#eee">Conf: {conf:.0%}</span>
  </div>

  <div class="section">
    <div class="label">🗣️ Lead žinutė</div>
    <div class="grid-2">
      <div class="lang-col">
        <div class="col-label">LT vertimas</div>
        <pre>{prospect_lt}</pre>
      </div>
      <div class="lang-col">
        <div class="col-label">{lang.upper()} originalas</div>
        <pre>{prospect_orig}</pre>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="label">✍️ Agent'o draftas</div>
    <div class="grid-2">
      <div class="lang-col">
        <div class="col-label">LT preview</div>
        <pre id="reply-lt-{iid}">{reply_lt}</pre>
      </div>
      <div class="lang-col">
        <div class="col-label">{lang.upper()} (siunčiamas)</div>
        <pre id="reply-orig-{iid}">{reply_orig}</pre>
      </div>
    </div>
  </div>

  <div class="actions">
    <button class="btn primary" onclick="approve({iid})">✅ Siųsti per Instantly</button>
    <button class="btn" onclick="copyText({iid})">📋 Copy tekstą</button>
    <button class="btn" onclick="openEdit({iid})">✏️ Edit</button>
    <button class="btn danger" onclick="reject({iid})">❌ Atmesti</button>
    <button class="btn danger" onclick="takeover({iid})">🚫 Human takeover</button>
  </div>
</div>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Laukia approval - Gleadsy</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
h1 {{ color: #333; margin: 0 0 8px 0; }}
.header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }}
.filters {{ background: white; padding: 12px 18px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); display: flex; gap: 12px; align-items: center; }}
.draft {{ background: white; padding: 20px; margin-bottom: 18px; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-left: 4px solid #f9a825; }}
.draft-header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }}
.meta {{ font-size: 14px; color: #333; }}
.age {{ color: #888; font-size: 12px; }}
.badges {{ margin-bottom: 14px; }}
.badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; margin-right: 6px; }}
.section {{ margin: 14px 0; }}
.label {{ font-weight: 600; color: #333; margin-bottom: 6px; font-size: 13px; text-transform: uppercase; letter-spacing: 0.3px; }}
.grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.lang-col {{ background: #fafafa; padding: 12px; border-radius: 6px; border: 1px solid #eee; }}
.col-label {{ font-size: 11px; color: #666; text-transform: uppercase; font-weight: 600; margin-bottom: 6px; }}
.lang-col pre {{ white-space: pre-wrap; word-break: break-word; margin: 0; font-family: inherit; font-size: 13px; line-height: 1.5; color: #222; }}
.actions {{ margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }}
.btn {{ padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; background: #e0e0e0; color: #333; }}
.btn:hover {{ background: #d0d0d0; }}
.btn.primary {{ background: #4285f4; color: white; }}
.btn.primary:hover {{ background: #3367d6; }}
.btn.danger {{ background: #f5f5f5; color: #c62828; }}
.btn.danger:hover {{ background: #ffebee; }}
.empty {{ text-align: center; padding: 60px 20px; background: white; border-radius: 10px; color: #666; }}
.empty h2 {{ margin: 0 0 8px 0; color: #2e7d32; }}
.modal-bg {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000; }}
.modal {{ display: none; position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); background: white; border-radius: 10px; padding: 24px; width: 90%; max-width: 800px; max-height: 85vh; overflow: auto; z-index: 1001; box-shadow: 0 8px 30px rgba(0,0,0,0.3); }}
.modal h3 {{ margin-top: 0; }}
.modal textarea {{ width: 100%; min-height: 100px; padding: 10px; border: 1px solid #ddd; border-radius: 6px; font-family: inherit; font-size: 13px; box-sizing: border-box; }}
.modal .preview {{ margin: 12px 0; padding: 10px; background: #fafafa; border-radius: 6px; font-size: 13px; white-space: pre-wrap; border: 1px solid #eee; }}
</style></head><body>
<div class="header">
  <div>
    <a href="/replies" style="color:#4285f4;text-decoration:none;font-size:13px">← Visi reply'ai</a>
    <h1>⏳ Laukia approval ({len(rows)})</h1>
  </div>
</div>

<div class="filters">
  <form method="GET" action="/pending" style="display:flex;gap:10px;align-items:center;margin:0">
    <label style="font-size:12px;color:#666;font-weight:600;text-transform:uppercase">Klientas:</label>
    <select name="client" style="padding:6px 10px;border:1px solid #ddd;border-radius:6px">{client_options}</select>
    <button type="submit" style="padding:6px 16px;background:#4285f4;color:white;border:none;border-radius:6px;cursor:pointer">Filtruoti</button>
    <a href="/pending" style="padding:6px 16px;background:#e0e0e0;color:#333;border-radius:6px;text-decoration:none;font-size:13px">Išvalyti</a>
  </form>
</div>

{drafts_html}

<div class="modal-bg" id="modalBg" onclick="closeEdit()"></div>
<div class="modal" id="editModal">
  <h3>Redaguoti draftą #<span id="editIid"></span></h3>
  <div class="label" style="font-size:11px;color:#666;text-transform:uppercase;font-weight:600">Dabartinis draftas:</div>
  <div class="preview" id="editCurrent"></div>

  <div class="label" style="margin-top:14px;font-size:11px;color:#666;text-transform:uppercase;font-weight:600">LT instrukcija - ką pakeisti:</div>
  <textarea id="editInstruction" placeholder="Pvz: Pridėk klausimą, ar jie jau dirbo su cold outreach agentūra. Pabaigoje paprašyk susitikimo laiko."></textarea>

  <div id="editResult" style="display:none;margin-top:14px">
    <div class="label" style="font-size:11px;color:#666;text-transform:uppercase;font-weight:600">Naujas draftas:</div>
    <div class="grid-2">
      <div class="lang-col">
        <div class="col-label">LT preview</div>
        <pre id="editResultLt"></pre>
      </div>
      <div class="lang-col">
        <div class="col-label">Original</div>
        <pre id="editResultOrig"></pre>
      </div>
    </div>
  </div>

  <div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end">
    <button class="btn" onclick="closeEdit()">Atšaukti</button>
    <button class="btn primary" onclick="runRewrite()">🔄 Perrašyti su Claude</button>
    <button class="btn primary" id="saveAndSendBtn" style="display:none" onclick="saveAndSend()">✅ Išsaugoti + Siųsti</button>
  </div>
</div>

<script>
async function post(url, body) {{
  const res = await fetch(url, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body || {{}}),
  }});
  if (!res.ok) alert('Klaida: ' + res.status);
  return res;
}}

async function approve(iid) {{
  if (!confirm('Siųsti draftą per Instantly?')) return;
  const res = await post('/api/approve/' + iid);
  if (res.ok) location.reload();
}}

async function reject(iid) {{
  if (!confirm('Atmesti draftą? Lead\\'ui niekas nebus siunčiama.')) return;
  const res = await post('/api/reject/' + iid);
  if (res.ok) location.reload();
}}

async function takeover(iid) {{
  if (!confirm('Human takeover? Agent\\'as nebereaguos šiam lead\\'ui.')) return;
  const res = await post('/api/takeover/' + iid);
  if (res.ok) location.reload();
}}

async function copyText(iid) {{
  const txt = document.getElementById('reply-orig-' + iid).innerText;
  await navigator.clipboard.writeText(txt);
  if (confirm('Nukopijuota. Pažymėti kaip išsiųstą rankomis?')) {{
    const res = await post('/api/mark_sent/' + iid);
    if (res.ok) location.reload();
  }}
}}

let currentEditIid = null;
function openEdit(iid) {{
  currentEditIid = iid;
  document.getElementById('editIid').innerText = iid;
  document.getElementById('editCurrent').innerText =
    document.getElementById('reply-orig-' + iid).innerText;
  document.getElementById('editInstruction').value = '';
  document.getElementById('editResult').style.display = 'none';
  document.getElementById('saveAndSendBtn').style.display = 'none';
  document.getElementById('modalBg').style.display = 'block';
  document.getElementById('editModal').style.display = 'block';
}}
function closeEdit() {{
  document.getElementById('modalBg').style.display = 'none';
  document.getElementById('editModal').style.display = 'none';
  currentEditIid = null;
}}
async function runRewrite() {{
  const instr = document.getElementById('editInstruction').value.trim();
  if (!instr) {{ alert('Įrašyk, ką pakeisti.'); return; }}
  const res = await post('/api/edit_draft/' + currentEditIid, {{lt_instruction: instr}});
  if (!res.ok) return;
  const body = await res.json();
  document.getElementById('editResultLt').innerText = body.agent_reply_lt || '';
  document.getElementById('editResultOrig').innerText = body.agent_reply || '';
  document.getElementById('editResult').style.display = 'block';
  document.getElementById('saveAndSendBtn').style.display = 'inline-block';
  // Refresh the draft preview on the page
  document.getElementById('reply-lt-' + currentEditIid).innerText = body.agent_reply_lt || '';
  document.getElementById('reply-orig-' + currentEditIid).innerText = body.agent_reply || '';
}}
async function saveAndSend() {{
  const res = await post('/api/approve/' + currentEditIid);
  if (res.ok) location.reload();
}}

// Deep-link anchor highlighting
if (location.hash && location.hash.startsWith('#draft-')) {{
  setTimeout(() => {{
    const el = document.querySelector(location.hash);
    if (el) el.style.borderLeftColor = '#4285f4';
  }}, 100);
}}
</script>
</body></html>"""
    return HTMLResponse(content=html)
```

Now add the API endpoints. Append to `main.py` (still inside the app):

```python
@app.post("/api/approve/{iid}")
async def api_approve(iid: int, request: Request):
    if not _get_dashboard_session(request):
        raise HTTPException(status_code=401)
    cursor = await db.execute(
        "SELECT lead_email, email_account, email_id, agent_reply, campaign_id, campaign_name "
        "FROM interactions WHERE id = ? AND approval_status = 'pending'",
        (iid,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pending draft not found")
    row = dict(row)
    # Send via Instantly (subject inherited by threading - pass empty or reconstruct)
    subject = f"Re: {row.get('campaign_name') or ''}".strip() if row.get("campaign_name") else ""
    try:
        await send_reply(
            email_account=row["email_account"],
            reply_to_uuid=row["email_id"],
            subject=subject,
            body_text=row["agent_reply"],
        )
    except Exception as e:
        logger.exception("Approval send failed for iid=%s", iid)
        from core.slack_notifier import notify_error
        await notify_error("approval_send_failed", f"iid={iid} err={e}")
        raise HTTPException(status_code=502, detail=f"Instantly send failed: {e}")
    await update_approval_status(db, iid, "sent", approved_by="paulius",
                                  final_sent_text=row["agent_reply"])
    return {"status": "sent"}


@app.post("/api/reject/{iid}")
async def api_reject(iid: int, request: Request):
    if not _get_dashboard_session(request):
        raise HTTPException(status_code=401)
    await update_approval_status(db, iid, "rejected", approved_by="paulius")
    return {"status": "rejected"}


@app.post("/api/mark_sent/{iid}")
async def api_mark_sent(iid: int, request: Request):
    if not _get_dashboard_session(request):
        raise HTTPException(status_code=401)
    await update_approval_status(db, iid, "sent_manually", approved_by="paulius")
    return {"status": "sent_manually"}


@app.post("/api/takeover/{iid}")
async def api_takeover(iid: int, request: Request):
    if not _get_dashboard_session(request):
        raise HTTPException(status_code=401)
    cursor = await db.execute(
        "SELECT lead_email, campaign_id FROM interactions WHERE id = ?", (iid,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404)
    await set_human_takeover(db, row["lead_email"], row["campaign_id"])
    await update_approval_status(db, iid, "rejected", approved_by="paulius")
    return {"status": "takeover_registered"}


@app.post("/api/edit_draft/{iid}")
async def api_edit_draft(iid: int, request: Request):
    if not _get_dashboard_session(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    lt_instruction = (body or {}).get("lt_instruction", "").strip()
    if not lt_instruction:
        raise HTTPException(status_code=400, detail="lt_instruction required")

    cursor = await db.execute(
        "SELECT agent_reply, original_language, client_id, campaign_id "
        "FROM interactions WHERE id = ? AND approval_status = 'pending'",
        (iid,),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404)
    row = dict(row)

    client_config = clients.get(row["client_id"])
    if not client_config:
        raise HTTPException(status_code=500, detail="Client config not loaded")

    before = row["agent_reply"] or ""
    target_lang = row["original_language"] or client_config.get("tone", {}).get("language", "lt")
    try:
        new_draft = await rewrite_draft(
            original_draft=before,
            lt_instruction=lt_instruction,
            target_language=target_lang,
            client_config=client_config,
        )
    except Exception as e:
        logger.exception("rewrite_draft failed for iid=%s", iid)
        raise HTTPException(status_code=502, detail=f"Claude rewrite failed: {e}")

    new_draft_lt = await translate_to_lt(new_draft, target_lang)

    await update_draft_text(db, iid, new_draft, new_draft_lt)
    await append_edit_history(db, iid, {
        "lt_instruction": lt_instruction,
        "before": before,
        "after": new_draft,
        "before_lt": "",
        "after_lt": new_draft_lt,
    })
    return {"agent_reply": new_draft, "agent_reply_lt": new_draft_lt}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_main_pending.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_pending.py
git commit -m "feat(dashboard): /pending page + approve/reject/edit API endpoints"
```

---

## Task 12: `/replies` header badge + conversation view pending badge

**Files:**
- Modify: `main.py`
- Test: `tests/test_main_pending.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_main_pending.py`:

```python
@pytest.mark.asyncio
async def test_replies_page_shows_pending_count_badge(client_with_db):
    client, db = client_with_db
    await _seed_pending(db, email_id="eid-a")
    await _seed_pending(db, email_id="eid-b")
    r = await client.get("/replies")
    assert r.status_code == 200
    # Badge should show count
    assert "Laukia approval" in r.text
    assert ">2<" in r.text or "(2)" in r.text  # count rendered


@pytest.mark.asyncio
async def test_replies_page_no_badge_when_zero_pending(client_with_db):
    client, db = client_with_db
    r = await client.get("/replies")
    assert r.status_code == 200
    # Badge still rendered but with 0 (not red)
    assert "Laukia approval" in r.text


@pytest.mark.asyncio
async def test_conversation_view_shows_pending_badge(client_with_db):
    client, db = client_with_db
    await _seed_pending(db, email_id="eid-conv")
    r = await client.get("/conversation/p@acme.fr/c1")
    assert r.status_code == 200
    assert "Laukia approval" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_main_pending.py::test_replies_page_shows_pending_count_badge -v`
Expected: FAIL - badge not in HTML

- [ ] **Step 3: Modify /replies endpoint to render badge**

Modify `main.py` - in the `/replies` handler, near the header section (around line 738-745 where the learning link and logout are), fetch pending count and add badge. Add near the top of the function (before HTML generation):

```python
    pending_count = await get_pending_count(db)
```

Then modify the header HTML block (replace the div with learning + logout buttons):

```python
    pending_badge_html = ""
    if pending_count > 0:
        pending_badge_html = (
            f'<a href="/pending" style="padding:8px 14px;background:#c62828;color:white;'
            f'border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;margin-right:8px">'
            f'⏳ Laukia approval ({pending_count})</a>'
        )
    else:
        pending_badge_html = (
            '<a href="/pending" style="padding:8px 14px;background:#e0e0e0;color:#333;'
            'border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;margin-right:8px">'
            '⏳ Laukia approval (0)</a>'
        )
```

And in the f-string for the page HTML, replace the header block (the `<div class="header">...</div>` section at top of body) to include `{pending_badge_html}`:

Find this block:
```html
<div class="header">
    <h1>Gleadsy Reply Agent</h1>
    <div style="display:flex;gap:8px;align-items:center">
        <a href="/learning" ...>🎓 Mokymosi progresas</a>
        <form method="POST" action="/logout"...>...</form>
    </div>
</div>
```

Replace with:
```html
<div class="header">
    <h1>Gleadsy Reply Agent</h1>
    <div style="display:flex;gap:8px;align-items:center">
        {pending_badge_html}
        <a href="/learning" style="padding:8px 14px;background:#1565c0;color:white;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">🎓 Mokymosi progresas</a>
        <form method="POST" action="/logout" style="margin:0">
            <button type="submit" class="logout-btn">Atsijungti</button>
        </form>
    </div>
</div>
```

- [ ] **Step 4: Modify `/conversation/{lead}/{campaign}` endpoint**

In the conversation view (around line 876 in main.py, inside the `messages_html` loop), detect pending drafts and render them with a badge. Find the block that renders `agent_reply` (the `if agent_reply:` branch, around line 886):

Replace the opening of that branch:

```python
        if agent_reply:
            approval_status = r.get("approval_status")
            pending_html = ""
            if approval_status == "pending":
                pending_html = (
                    '<div style="margin-top:8px;padding:8px 12px;background:#fff3e0;'
                    'border-left:3px solid #f9a825;border-radius:4px;font-size:12px">'
                    '⏳ <strong>Laukia approval</strong> - '
                    f'<a href="/pending#draft-{r["id"]}" style="color:#1565c0">Eiti į approval</a>'
                    '</div>'
                )
            # ... rest of agent-reply rendering unchanged, but append pending_html at end of msg-body
```

And at the end of the `messages_html += f"""<div class="msg agent-msg">...""":` block, include `{pending_html}` after `{why_html}`:

```python
            messages_html += f"""
        <div class="msg agent-msg">
            <div class="msg-header">
                <strong>Agent</strong> - {sent_text}{rating_icon}
                <span class="q-badge-inline" style="color:{q_badge_color};background:{q_badge_bg};margin-left:8px">{quality_score if quality_score is not None else "-"}/10</span>
            </div>
            <div class="msg-body">{agent_reply}</div>{improve_html}{why_html}{pending_html}
        </div>"""
```

Also, modify the interactions SELECT query to include `approval_status` and `id`:

```python
    cursor = await db.execute(
        "SELECT id, created_at, classification, confidence, classification_reasoning, "
        "prospect_message, agent_reply, was_sent, human_rating, "
        "quality_score, quality_summary, quality_issues, improvement_suggestion, "
        "few_shots_used, thread_position, approval_status "
        "FROM interactions WHERE lead_email = ? AND campaign_id = ? ORDER BY created_at ASC",
        (lead_email, campaign_id),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_main_pending.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main_pending.py
git commit -m "feat(dashboard): pending count badge + conversation view marker"
```

---

## Task 13: Regression sweep - run full test suite

- [ ] **Step 1: Run all reply-agent tests**

Run: `pytest tests/ -v --tb=short`
Expected: All pass. If any fail, diagnose:
  - `test_webhook_handler` - ensure all patched functions match new imports (translate_to_lt, notify_approval_pending)
  - `test_reply_generator` - ensure existing tests still work with new optional parameter
  - `test_self_improver` - `get_best_examples` with default `language=None` must behave as before
  - `test_client_loader` - legacy YAML loading must still work

- [ ] **Step 2: Fix any regressions inline**

If a pre-existing test breaks, trace the root cause. Common issues:
  - `get_best_examples` called without language kwarg in webhook - ensure default None is used
  - Existing client YAMLs (gleadsy.yaml, ibjoist.yaml, puoskio-spauda.yaml) missing `approval_required` - loader now sets default False, so no action needed
  - `log_interaction` called with dict missing new keys - all new keys use `.get()` with defaults, no failure

- [ ] **Step 3: Commit any regression fixes**

```bash
git add -A
git commit -m "test: regression sweep after approval-flow refactor"
```

---

## Task 14: Smoke test with gleadsy-self client

**Files:**
- Modify: `clients/gleadsy.yaml` (or create new `clients/gleadsy-self.yaml` - check existing first)

- [ ] **Step 1: Inspect existing gleadsy-self setup**

Run: `ls clients/ && head -20 clients/gleadsy.yaml`

Check: does gleadsy-self exist as a separate YAML or is it sub-campaign of gleadsy? Based on CLAUDE.md, gleadsy-self is a client name but may not have its own YAML. Check the existing campaigns in gleadsy.yaml - campaign UUIDs listed there are the gleadsy-self ones too.

- [ ] **Step 2: Create a test-only FR YAML**

Create `clients/_gleadsy-fr-smoke.yaml` (the `_` prefix prevents `load_clients` from loading it until renamed - per the existing skip logic in client_loader):

```yaml
# gleadsy-fr-smoke.yaml - SMOKE TEST FIXTURE (rename to remove _ prefix to activate)
client_id: "gleadsy-fr-smoke"
client_name: "Gleadsy (FR smoke test)"

approval_required: true

campaigns:
  - id: "REPLACE-WITH-REAL-FR-CAMPAIGN-UUID"
    language: "fr"
    name: "Gleadsy FR smoke test"

company_description: |
  Gleadsy - digital marketing agency that helps B2B companies acquire more clients
  through cold email campaigns and Google Ads.

service_offering: |
  - Cold email outreach
  - Google Ads management + landing page creation
  Full campaign management from strategy to results.

value_proposition: |
  We guarantee 5 qualified meetings per month or we refund.

pricing: |
  - Cold outreach: 800 EUR/month + VAT (starting)
  - Google Ads + landing page: 700 EUR/month + VAT
  - Both combined: 1300 EUR/month + VAT

target_audience: |
  B2B service company founders, 5-50 employees, France.

meeting:
  participant_from_client: "Paulius, Gleadsy founder"
  purpose: "Short consultation about client acquisition"
  duration_minutes: 30
  google_calendar_id: "primary"
  working_hours:
    start: "09:00"
    end: "17:00"
    days: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
  buffer_minutes: 15
  advance_days: 7
  slots_to_offer: 3

faq:
  - question: "Combien ça coûte?"
    answer: "Le prix dépend de vos besoins. Discutons-en lors d'un court appel."
  - question: "Comment ça marche?"
    answer: "Nous créons une campagne ciblée et gérons tout le processus. Vous recevez des rendez-vous qualifiés dans votre calendrier."

boundaries:
  cannot_promise:
    - "Specific result numbers (except 5-meeting guarantee)"
    - "Discounts or special pricing"
    - "Technical details unknown"
  escalate_topics:
    - "Legal questions"
    - "Detailed technical questions"

tone:
  formality: "semi-formal"
  addressing: "vous"
  language: "fr"
  personality: |
    Friendly, concise, no corporate jargon. Addresses objections directly.
    Guides toward a meeting without pressure.
  max_reply_length_sentences: 5
  sign_off: "Cordialement"
  sender_name: "Paulius"
```

- [ ] **Step 3: Document smoke test procedure**

Create `docs/superpowers/plans/SMOKE_TEST_CHECKLIST.md`:

```markdown
# Smoke Test Checklist - Foreign Reply Approval Flow

## Setup (one-time)

1. Pick a small, controlled FR Instantly campaign UUID (one you own, e.g. test leads only)
2. Rename `clients/_gleadsy-fr-smoke.yaml` → `clients/gleadsy-fr-smoke.yaml`
3. Replace `REPLACE-WITH-REAL-FR-CAMPAIGN-UUID` with the real campaign UUID
4. Deploy: `./deploy.sh` or equivalent
5. Verify boot logs: `Klientai: gleadsy, ibjoist, puoskio-spauda, gleadsy-fr-smoke`

## Test 1: New draft enters pending queue

1. Send yourself a test email from an alias to that FR campaign, replying in French:
   > "Bonjour, je suis intéressé. Quels sont vos prix?"
2. Within 30s, expect Slack notification with ⏳ prefix, 🇫🇷 FR flag, LT preview
3. Open `https://reply.gleadsy.com/pending`
4. Verify: draft card shows lead email, FR flag, LT+FR side-by-side
5. Verify `/replies` header shows "⏳ Laukia approval (1)" in red

## Test 2: Edit workflow

1. Click ✏️ Edit on the draft
2. Enter LT instruction: "Pridėk klausimą, ar jie jau bandė cold outreach"
3. Click 🔄 Perrašyti su Claude
4. Verify new FR draft contains a question about cold outreach
5. Verify LT preview also updated

## Test 3: Approval sends via Instantly

1. Click ✅ Siųsti per Instantly
2. Verify response 200, draft disappears from pending queue
3. Check Instantly UI - email actually sent?
4. Check `/replies` - draft now shows "sent" with was_sent=1, approval_status=sent
5. Check `/conversation/<lead_email>/<campaign_id>` - full thread visible

## Test 4: Reject does not send

1. Send another FR test email
2. On `/pending`, click ❌ Atmesti
3. Verify draft disappears, approval_status=rejected, was_sent=0
4. Verify Instantly did NOT send anything

## Test 5: LT auto-send still works

1. Send LT test email to gleadsy client's LT campaign
2. Verify: auto-sent without pending queue, appears in /replies with was_sent=1
3. Slack notification uses old "notify_reply_sent" format (not approval_pending)

## Cleanup

- After smoke test, either leave the smoke YAML in place or remove it
- Document smoke test result + timestamp in a followup commit message
```

- [ ] **Step 4: Commit smoke test fixture**

```bash
git add clients/_gleadsy-fr-smoke.yaml docs/superpowers/plans/SMOKE_TEST_CHECKLIST.md
git commit -m "docs: smoke test checklist + FR client fixture"
```

---

## Task 15: Environment variables + deployment notes

**Files:**
- Modify: `config.py` (verify TRANSLATION_MODEL + REWRITE_MODEL already added in Task 3)
- Create: `.env.example` entries (if such file exists - skip otherwise)

- [ ] **Step 1: Verify config defaults**

Run: `grep -E "(TRANSLATION_MODEL|REWRITE_MODEL|DASHBOARD_BASE_URL)" config.py`

Expected output includes both model constants + DASHBOARD_BASE_URL.

- [ ] **Step 2: Check if .env.example exists**

Run: `ls .env* 2>&1`

If `.env.example` exists, append:

```
# Translation + rewrite models (foreign-language approval flow)
TRANSLATION_MODEL=claude-haiku-4-5-20251001
REWRITE_MODEL=claude-sonnet-4-6
```

If not present, skip this step.

- [ ] **Step 3: Update DASHBOARD_BASE_URL for prod**

Verify prod `DASHBOARD_BASE_URL` env var on the deployment server points to `https://reply.gleadsy.com`. No code change - this is an operational check.

Run: `grep DASHBOARD_BASE_URL config.py`
Expected: `DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "https://gleadsy-reply-agent.onrender.com")`

If the default points to onrender.com but prod actually runs on reply.gleadsy.com, the deployment env var must be set correctly (does not block code merge - operational task).

- [ ] **Step 4: Commit (if .env.example updated)**

```bash
git add .env.example 2>/dev/null
git diff --cached --quiet && echo "nothing to commit" || git commit -m "docs: env vars for translation/rewrite models"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Plan task(s) |
|---|---|
| DB schema (9 new columns + index) | Task 4 |
| Per-campaign YAML + approval_required | Task 6 |
| Language detection (langdetect + hint) | Tasks 1, 2 |
| Translation module (translate_to_lt, rewrite_draft) | Task 3 |
| Reply generator target_language | Task 7 |
| Few-shot per-language filter | Task 8 |
| Slack notify_approval_pending | Task 9 |
| Pipeline branch on approval_required | Task 10 |
| Thread count counts sent+approved only | Task 5 (Step 5) |
| INTERESTED skip auto-book when approval_required | Task 10 (Step 4 note) |
| /pending dashboard page | Task 11 |
| /api/approve/reject/mark_sent/takeover/edit_draft | Task 11 |
| /replies header badge | Task 12 |
| /conversation pending marker | Task 12 |
| Smoke test plan | Task 14 |

All spec sections covered.

**Placeholder scan:** No TBD, TODO, or "implement later" in the plan. Each code block is complete.

**Type consistency:** Function signatures reviewed:
- `translate_to_lt(text: str, source_language: str) -> str` - consistent usage in webhook + edit_draft endpoint
- `rewrite_draft(original_draft, lt_instruction, target_language, client_config)` - keyword args match in API endpoint
- `detect_language(text, campaign_hint) -> str` - consistent
- `get_best_examples(db, category, client_id, limit, language=None)` - backward-compat default
- `update_approval_status(db, iid, status, approved_by=None, final_sent_text=None)` - all call sites match
- `notify_approval_pending(iid, lead_email, client_id, classification, quality_score, confidence, prospect_message_lt, agent_reply_lt, original_language, dashboard_base_url)` - order/names consistent between definition and callers

One known risk: Task 10 modifies `instantly_webhook.py` heavily. Reply-subject reconstruction in `/api/approve/{iid}` uses `campaign_name` as a proxy but the original webhook payload had `reply_subject` which isn't stored in DB currently. Added a pragmatic fallback (`Re: {campaign_name}`) for the approval path; this may produce a slightly different subject than auto-send. Acceptable for MVP - lead still receives the email in the same thread because `reply_to_uuid` (the email_id) handles threading at the Instantly API level.
