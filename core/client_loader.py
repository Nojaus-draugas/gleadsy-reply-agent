import yaml
from pathlib import Path


REQUIRED_FIELDS = [
    "client_id", "client_name", "campaigns", "company_description",
    "service_offering", "value_proposition", "pricing", "target_audience",
    "meeting", "faq", "boundaries", "tone",
]


def load_clients(clients_dir: Path) -> dict:
    """Load all YAML client configs from directory. Returns {client_id: config}."""
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
        clients[config["client_id"]] = config
    return clients


def get_client_by_campaign(clients: dict, campaign_id: str) -> dict | None:
    """Find client config by Instantly campaign UUID."""
    for client in clients.values():
        campaigns = client.get("campaigns", [])
        for camp in campaigns:
            # Support both dict format {"id": "...", "name": "..."} and plain string
            camp_id = camp["id"] if isinstance(camp, dict) else camp
            if camp_id == campaign_id:
                return client
    return None
