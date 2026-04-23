import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch
import main


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    # Override DB_PATH and disable dashboard auth for tests
    import importlib, config

    # Save original values to restore after test
    orig_dashboard_pw = config.DASHBOARD_PASSWORD
    orig_webhook_secret = config.WEBHOOK_SECRET
    orig_db_path = config.DB_PATH

    # Directly override config attrs (load_dotenv override=True re-reads .env on reload)
    config.DASHBOARD_PASSWORD = ""
    config.WEBHOOK_SECRET = ""
    config.DB_PATH = tmp_path / "test.db"

    # Start lifespan manually
    async with main.app.router.lifespan_context(main.app):
        async with AsyncClient(transport=ASGITransport(app=main.app),
                                base_url="http://test") as ac:
            yield ac, main.db

    # Restore config so other tests aren't affected
    config.DASHBOARD_PASSWORD = orig_dashboard_pw
    config.WEBHOOK_SECRET = orig_webhook_secret
    config.DB_PATH = orig_db_path


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
