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
    # approval_required defaults to True (drafts created, not auto-sent)
    assert clients["legacy_client"]["approval_required"] is True


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
