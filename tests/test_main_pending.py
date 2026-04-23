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
         patch("main.translate_to_lt", new=AsyncMock(return_value="Ačiū! Kada?")), \
         patch("main.review_quality", new=AsyncMock(return_value=type("Q", (), {
             "score": 8, "passed": True, "issues": [], "summary": "ok",
             "improvement_suggestion": "",
         })())), \
         patch("main.check_hallucinations", return_value=[]):
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


@pytest.mark.asyncio
async def test_edit_draft_reruns_quality_and_hallucination_checks(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)

    # Mock Claude rewrite + translate + quality review + hallucination guard
    fake_quality = type("Q", (), {
        "score": 7, "passed": True,
        "issues": ["shortened a sentence"], "summary": "decent rewrite",
        "improvement_suggestion": "",
    })()

    with patch("main.rewrite_draft", new=AsyncMock(return_value="Nouveau texte")), \
         patch("main.translate_to_lt", new=AsyncMock(return_value="Naujas tekstas")), \
         patch("main.review_quality", new=AsyncMock(return_value=fake_quality)) as mock_qr, \
         patch("main.check_hallucinations", return_value=[]) as mock_halluc:
        main.clients["gleadsy_fr"] = {
            "client_id": "gleadsy_fr", "client_name": "Gleadsy FR",
            "tone": {"language": "fr", "sign_off": "Cordialement", "sender_name": "P",
                     "personality": "x"},
            "approval_required": True,
        }
        r = await client.post(f"/api/edit_draft/{iid}",
                               json={"lt_instruction": "Pakeisk"})

    assert r.status_code == 200
    body = r.json()
    # Response must include the new quality score + summary
    assert body["quality_score"] == 7
    assert body["quality_summary"] == "decent rewrite"
    assert body["hallucination_issues"] == []

    # Both checks were called
    mock_qr.assert_called_once()
    mock_halluc.assert_called_once()

    # DB row must reflect new quality values
    cursor = await db.execute(
        "SELECT quality_score, quality_summary FROM interactions WHERE id = ?", (iid,)
    )
    row = dict(await cursor.fetchone())
    assert row["quality_score"] == 7
    assert row["quality_summary"] == "decent rewrite"


@pytest.mark.asyncio
async def test_edit_draft_surfaces_hallucination_issues(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    fake_quality = type("Q", (), {
        "score": 3, "passed": False,
        "issues": ["hallucinated phone"], "summary": "invented phone number",
        "improvement_suggestion": "",
    })()

    with patch("main.rewrite_draft", new=AsyncMock(return_value="Call me at +37061111111")), \
         patch("main.translate_to_lt", new=AsyncMock(return_value="Skambink")), \
         patch("main.review_quality", new=AsyncMock(return_value=fake_quality)), \
         patch("main.check_hallucinations", return_value=["phone +37061111111 not in brief"]):
        main.clients["gleadsy_fr"] = {
            "client_id": "gleadsy_fr", "client_name": "Gleadsy FR",
            "tone": {"language": "fr", "sign_off": "Cordialement", "sender_name": "P",
                     "personality": "x"},
            "approval_required": True,
        }
        r = await client.post(f"/api/edit_draft/{iid}",
                               json={"lt_instruction": "Pridėk telefoną"})
    assert r.status_code == 200
    body = r.json()
    # Even though quality failed, we DON'T block - we surface info
    assert body["quality_score"] == 3
    assert body["hallucination_issues"] == ["phone +37061111111 not in brief"]
    # Draft was still saved (user decides)
    cursor = await db.execute("SELECT agent_reply FROM interactions WHERE id = ?", (iid,))
    assert (await cursor.fetchone())["agent_reply"] == "Call me at +37061111111"


@pytest.mark.asyncio
async def test_replies_page_shows_pending_count_badge(client_with_db):
    client, db = client_with_db
    await _seed_pending(db, email_id="eid-a")
    await _seed_pending(db, email_id="eid-b")
    r = await client.get("/replies")
    assert r.status_code == 200
    assert "Laukia approval" in r.text
    # Count should appear somewhere in the badge - either "(2)" or ">2<"
    assert "(2)" in r.text or ">2<" in r.text


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


@pytest.mark.asyncio
async def test_approve_returns_409_on_second_concurrent_click(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    send_calls = {"count": 0}
    async def mock_send(**kwargs):
        send_calls["count"] += 1
        return {}
    with patch("main.send_reply", new=mock_send):
        # First call wins - should succeed
        r1 = await client.post(f"/api/approve/{iid}", json={})
        # Immediately call again - draft is now 'sent', not pending. Expected 404 or 409.
        r2 = await client.post(f"/api/approve/{iid}", json={})
    assert r1.status_code == 200
    assert r2.status_code in (404, 409)  # pending row gone on second call
    assert send_calls["count"] == 1  # Instantly was only called once


@pytest.mark.asyncio
async def test_approve_restores_pending_on_instantly_failure(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    async def failing_send(**kwargs):
        raise RuntimeError("Instantly down")
    with patch("main.send_reply", new=failing_send):
        r = await client.post(f"/api/approve/{iid}", json={})
    assert r.status_code == 502
    # Draft must be back to pending so user can retry
    cursor = await db.execute(
        "SELECT approval_status FROM interactions WHERE id = ?", (iid,)
    )
    assert (await cursor.fetchone())["approval_status"] == "pending"


@pytest.mark.asyncio
async def test_edit_draft_returns_409_if_already_approved(client_with_db):
    client, db = client_with_db
    iid = await _seed_pending(db)
    # Simulate approval happening between modal open and rewrite
    from db.database import update_approval_status
    await update_approval_status(db, iid, "sent", approved_by="paulius")

    with patch("main.rewrite_draft", new=AsyncMock()) as mock_rw, \
         patch("main.translate_to_lt", new=AsyncMock()):
        main.clients["gleadsy_fr"] = {
            "client_id": "gleadsy_fr", "client_name": "Gleadsy FR",
            "tone": {"language": "fr", "sign_off": "Cordialement", "sender_name": "P", "personality": "x"},
            "approval_required": True,
        }
        r = await client.post(f"/api/edit_draft/{iid}",
                               json={"lt_instruction": "anything"})
    # Should NOT have called Claude - spec says guard first
    mock_rw.assert_not_called()
    assert r.status_code in (404, 409)


@pytest.mark.asyncio
async def test_approve_uses_stored_reply_subject(client_with_db):
    client, db = client_with_db
    # Seed with a specific reply_subject stored
    from db.database import log_interaction
    iid = await log_interaction(db, {
        "campaign_id": "c1", "lead_email": "p@acme.fr",
        "email_id": "eid-subj-test", "client_id": "gleadsy_fr",
        "prospect_message": "Bonjour", "classification": "QUESTION",
        "confidence": 0.9, "agent_reply": "Merci",
        "approval_status": "pending", "was_sent": False,
        "reply_subject": "Question sur vos services",
    })

    sent_args = {}
    async def capture_send(**kwargs):
        sent_args.update(kwargs)
        return {}

    with patch("main.send_reply", new=capture_send):
        r = await client.post(f"/api/approve/{iid}", json={})
    assert r.status_code == 200
    # Subject prepended with "Re: " since stored subject doesn't already have it
    assert sent_args["subject"] == "Re: Question sur vos services"


@pytest.mark.asyncio
async def test_approve_does_not_double_prefix_re(client_with_db):
    client, db = client_with_db
    from db.database import log_interaction
    iid = await log_interaction(db, {
        "campaign_id": "c1", "lead_email": "p@acme.fr",
        "email_id": "eid-re-test", "client_id": "gleadsy_fr",
        "prospect_message": "Bonjour", "classification": "QUESTION",
        "confidence": 0.9, "agent_reply": "Merci",
        "approval_status": "pending", "was_sent": False,
        "reply_subject": "Re: Question sur vos services",  # already has Re:
    })

    sent_args = {}
    async def capture_send(**kwargs):
        sent_args.update(kwargs)
        return {}

    with patch("main.send_reply", new=capture_send):
        r = await client.post(f"/api/approve/{iid}", json={})
    assert r.status_code == 200
    # No double "Re: Re: " prefix
    assert sent_args["subject"] == "Re: Question sur vos services"


@pytest.mark.asyncio
async def test_approve_falls_back_to_campaign_name_if_no_subject_stored(client_with_db):
    client, db = client_with_db
    from db.database import log_interaction
    iid = await log_interaction(db, {
        "campaign_id": "c1", "campaign_name": "Gleadsy FR outreach",
        "lead_email": "p@acme.fr", "email_id": "eid-fallback",
        "client_id": "gleadsy_fr",
        "prospect_message": "Bonjour", "classification": "QUESTION",
        "confidence": 0.9, "agent_reply": "Merci",
        "approval_status": "pending", "was_sent": False,
        # reply_subject intentionally omitted
    })

    sent_args = {}
    async def capture_send(**kwargs):
        sent_args.update(kwargs)
        return {}

    with patch("main.send_reply", new=capture_send):
        r = await client.post(f"/api/approve/{iid}", json={})
    assert r.status_code == 200
    assert sent_args["subject"] == "Re: Gleadsy FR outreach"
