import os
os.environ.setdefault("TIMEZONE", "Europe/Vilnius")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("INSTANTLY_API_KEY", "test-key")
os.environ["TEST_MODE"] = "false"

# .env turi TEST_MODE=true (defaultina į drafts-only gamyboje), bet testuose
# siunčiame tik per mock'intą send_reply. config.load_dotenv(override=True)
# perrašo os.environ, todėl tiesiogiai override'inam modulio atributą.
import config
config.TEST_MODE = False
