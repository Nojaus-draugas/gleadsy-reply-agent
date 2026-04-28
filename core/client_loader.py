import yaml
from pathlib import Path


REQUIRED_FIELDS = [
    "client_id", "client_name", "campaigns", "company_description",
    "service_offering", "value_proposition", "pricing", "target_audience",
    "meeting", "faq", "boundaries", "tone",
]


def load_clients(clients_dir: Path) -> dict:
    """Load all YAML client configs from directory. Returns {client_id: config}.

    Each client dict gets `approval_required: bool` (default True) explicitly set.
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
        config.setdefault("approval_required", True)
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
