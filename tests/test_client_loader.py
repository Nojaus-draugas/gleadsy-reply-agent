import pytest
from pathlib import Path
from core.client_loader import load_clients, get_client_by_campaign


@pytest.fixture
def clients(tmp_path):
    yaml_content = """
client_id: "test_client"
client_name: "Test Client"
campaigns:
  - "campaign-uuid-1"
  - "campaign-uuid-2"
company_description: "Test company"
service_offering: "Test service"
value_proposition: "Test value"
pricing: "Test pricing"
target_audience: "Test audience"
meeting:
  participant_from_client: "Test Person"
  purpose: "Test meeting"
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
  - question: "Test question?"
    answer: "Test answer"
boundaries:
  cannot_promise:
    - "Nothing"
  escalate_topics:
    - "Legal"
tone:
  formality: "semi-formal"
  addressing: "Jūs"
  language: "lt"
  personality: "Friendly"
  max_reply_length_sentences: 5
  sign_off: "Pagarbiai"
  sender_name: "Test"
"""
    (tmp_path / "test_client.yaml").write_text(yaml_content, encoding="utf-8")
    return load_clients(tmp_path)


def test_load_clients(clients):
    assert "test_client" in clients
    assert clients["test_client"]["client_name"] == "Test Client"


def test_get_client_by_campaign(clients):
    result = get_client_by_campaign(clients, "campaign-uuid-1")
    assert result is not None
    assert result["client_id"] == "test_client"


def test_get_client_unknown_campaign(clients):
    result = get_client_by_campaign(clients, "unknown-uuid")
    assert result is None


def test_client_has_required_fields(clients):
    c = clients["test_client"]
    assert "meeting" in c
    assert c["meeting"]["duration_minutes"] == 30
    assert len(c["faq"]) == 1
    assert c["tone"]["language"] == "lt"
