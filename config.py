import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.getenv("EPETREL_DB_PATH", os.path.join(BASE_DIR, "database", "storage.db"))

MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "MutualWarm")
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "40"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
DEFAULT_SYSTEM_PROMPT = os.getenv(
    "DEFAULT_SYSTEM_PROMPT",
    (
        "You write plain, low-stakes mailbox warm conversation content. "
        "Create short, normal messages that sound like real people writing casual work notes or light everyday check-ins. "
        "Never write sales outreach, promotions, lead generation, deliverability language, spam-filter language, or anything that reveals automation. "
        "Keep subjects short, bodies concise, safe, and context-aware."
    ),
)

EPETREL_SITE_URL = os.getenv("EPETREL_SITE_URL", "https://epetrel.com")
EPETREL_BFF_BASE_URL = os.getenv("EPETREL_BFF_BASE_URL", "https://bff.epetrel.com")

WARM_SCAN_SOFT_TIMEOUT_HOURS = int(os.getenv("WARM_SCAN_SOFT_TIMEOUT_HOURS", "24"))
WARM_SCAN_HARD_TIMEOUT_HOURS = int(os.getenv("WARM_SCAN_HARD_TIMEOUT_HOURS", "48"))
WARM_REPLY_MIN_DELAY_HOURS = int(os.getenv("WARM_REPLY_MIN_DELAY_HOURS", "2"))
WARM_REPLY_HARD_TIMEOUT_HOURS = int(os.getenv("WARM_REPLY_HARD_TIMEOUT_HOURS", "48"))
WARM_SLEEP_START_HOUR = int(os.getenv("WARM_SLEEP_START_HOUR", "22"))
WARM_SLEEP_END_HOUR = int(os.getenv("WARM_SLEEP_END_HOUR", "7"))
WARM_AVOID_WEEKENDS = os.getenv("WARM_AVOID_WEEKENDS", "1").strip().lower() not in {"0", "false", "no"}
WARM_LOCAL_TIMEZONE = os.getenv("WARM_LOCAL_TIMEZONE", os.getenv("TZ", "Asia/Shanghai"))
WARM_WORKER_ENABLED = os.getenv("WARM_WORKER_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
WARM_WORKER_INTERVAL_SECONDS = int(os.getenv("WARM_WORKER_INTERVAL_SECONDS", "300"))
WARM_TASK_CLAIM_LIMIT = int(os.getenv("WARM_TASK_CLAIM_LIMIT", "1"))
WARM_SEND_MIN_GAP_SECONDS = int(os.getenv("WARM_SEND_MIN_GAP_SECONDS", "180"))
WARM_SEND_MAX_GAP_SECONDS = int(os.getenv("WARM_SEND_MAX_GAP_SECONDS", "480"))
WARM_PROBE_SCAN_TIMEOUT_SECONDS = int(os.getenv("WARM_PROBE_SCAN_TIMEOUT_SECONDS", "90"))
WARM_PROBE_SCAN_MIN_INTERVAL_SECONDS = int(os.getenv("WARM_PROBE_SCAN_MIN_INTERVAL_SECONDS", "7"))
WARM_PROBE_SCAN_MAX_INTERVAL_SECONDS = int(os.getenv("WARM_PROBE_SCAN_MAX_INTERVAL_SECONDS", "15"))
WARM_PROBE_RESCAN_TIMEOUT_SECONDS = int(os.getenv("WARM_PROBE_RESCAN_TIMEOUT_SECONDS", "45"))
WARM_PROBE_REPLY_MIN_DELAY_SECONDS = int(os.getenv("WARM_PROBE_REPLY_MIN_DELAY_SECONDS", "12"))
WARM_PROBE_REPLY_MAX_DELAY_SECONDS = int(os.getenv("WARM_PROBE_REPLY_MAX_DELAY_SECONDS", "45"))
WARM_MAILBOX_OFFLINE_WARN_SEC = int(os.getenv("WARM_MAILBOX_OFFLINE_WARN_SEC", "3600"))
WARM_MAILBOX_STALE_SEC = int(os.getenv("WARM_MAILBOX_STALE_SEC", "259200"))
WARM_ALLOWED_EMAIL_DOMAINS = [
    item.strip().lower()
    for item in os.getenv("WARM_ALLOWED_EMAIL_DOMAINS", "gmail.com,googlemail.com").split(",")
    if item.strip()
]
