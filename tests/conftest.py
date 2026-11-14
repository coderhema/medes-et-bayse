import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Never talk to Telegram or place live orders from tests; never need real keys.
for key in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_CHAT_ID", "TAVILY_API_KEY", "LLM_API_KEY"):
    os.environ.pop(key, None)
os.environ["DRY_RUN"] = "true"
os.environ.setdefault("SLM_DISABLE_TEXT", "true")
