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
