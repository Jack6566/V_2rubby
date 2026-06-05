"""All settings are loaded from the .env file (never hard-coded)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---- Telegram ----
API_ID = _int("API_ID")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = _int("OWNER_ID")
LOG_GROUP_ID = _int("LOG_GROUP_ID")

# ---- Sending ----
# Hard bounds for the per-message delay (seconds).
MIN_DELAY = 0.2
MAX_DELAY = 10.0
DEFAULT_DELAY = _float("SEND_DELAY", 1.0)

# Marker at the end of the caption of the message in the account's Saved Messages.
FORWARD_MARKER = os.getenv("FORWARD_MARKER", "کد135").strip()

# Stop the whole run after this many failed sends.
MAX_ERRORS = _int("MAX_ERRORS", 3)

# Per-send timeout so a single stuck send can never hang the whole run.
SEND_TIMEOUT = _int("SEND_TIMEOUT", 60)

# Version label shown in the startup "Online" log card.
VERSION = os.getenv("VERSION", "V1")

# Only this id may control the bot.
ALLOWED_IDS = [i for i in [OWNER_ID] if i]


def clamp_delay(value) -> float:
    """Keep the configured delay inside [MIN_DELAY, MAX_DELAY]."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DELAY
    return max(MIN_DELAY, min(MAX_DELAY, value))


def validate() -> list:
    """Return a list of missing required settings (empty list = OK)."""
    problems = []
    if not API_ID:
        problems.append("API_ID")
    if not API_HASH:
        problems.append("API_HASH")
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN")
    if not OWNER_ID:
        problems.append("OWNER_ID")
    if not LOG_GROUP_ID:
        problems.append("LOG_GROUP_ID")
    return problems
