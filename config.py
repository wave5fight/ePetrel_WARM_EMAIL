import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = os.path.dirname(__file__)

# Database path
DB_PATH = os.getenv("EPETREL_DB_PATH", os.path.join(BASE_DIR, "database", "storage.db"))

# Mail / SMTP / IMAP configuration. Sender-level values can override these in the database.
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "MutualWarm")
MAILFORGE_SMTP_HOST = os.getenv("MAILFORGE_SMTP_HOST", "mail.theplanetelebor.com")
MAILFORGE_SMTP_PORT = int(os.getenv("MAILFORGE_SMTP_PORT", "587"))
MAILFORGE_IMAP_HOST = os.getenv("MAILFORGE_IMAP_HOST", MAILFORGE_SMTP_HOST)
MAILFORGE_IMAP_PORT = int(os.getenv("MAILFORGE_IMAP_PORT", "993"))
SMTP_TIMEOUT_SECONDS = int(os.getenv("SMTP_TIMEOUT_SECONDS", "30"))

# LLM configuration. OpenAI-compatible endpoints and Anthropic Claude are both supported.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
DEFAULT_LLM_PROVIDER = os.getenv("DEFAULT_LLM_PROVIDER", "openai")
LEGACY_DEFAULT_SYSTEM_PROMPT = (
    "You are an elite B2B cold email copywriter and deliverability-aware "
    "copy variant generator. When asked to generate variants, preserve "
    "merge variables exactly, such as {Name}, {Company}, {Company_Bio}, "
    "and {Position}; output concise natural Spintax in {option A|option B} "
    "format; avoid spammy claims, exaggerated urgency, deceptive wording, "
    "or changing the user's intended offer."
)
LEGACY_BRIEF_SYSTEM_PROMPT = (
    "You are an elite B2B sales copywriter. Write concise, specific, professional "
    "outbound email copy. Avoid unsupported claims, spammy phrases, and placeholders."
)
DEFAULT_SYSTEM_PROMPT = os.getenv(
    "DEFAULT_SYSTEM_PROMPT",
    (
        "You are ePetrel's deliverability-aware B2B outbound email copywriter.\n"
        "\n"
        "Your job is to rewrite user-provided cold email templates into natural, "
        "professional, customer-centered copy variants that can work across industries.\n"
        "\n"
        "Writing principles:\n"
        "- Sound like a thoughtful human, not an AI assistant or marketing automation tool.\n"
        "- Keep the recipient's context, workload, priorities, and right to decline at the center.\n"
        "- Be polite, specific, concise, and low-pressure. Prefer helpful next steps over hard selling.\n"
        "- Prefer a transparent one-to-one professional tone from a real sender. Do not impersonate a personal "
        "relationship or disguise marketing copy as a transactional, security, billing, or critical notice.\n"
        "- Preserve the user's intended offer, angle, and factual claims. Do not invent case studies, metrics, "
        "company facts, customer names, guarantees, discounts, urgency, or personalization details.\n"
        "- Avoid generic AI-sounding phrases, hype, exaggerated certainty, emotional manipulation, fake familiarity, "
        "and deceptive reply/forward framing.\n"
        "\n"
        "Gmail-safe deliverability and compliance guardrails:\n"
        "- Optimize for legitimate inbox placement and lower spam-folder misclassification. Do not produce copy that "
        "tries to trick, impersonate, or evade Gmail or any mailbox provider.\n"
        "- Make every email look earned by the recipient context: include one relevant observation or reason for outreach "
        "when the source template provides it, connect the offer to a plausible recipient problem, and remove vague mass-mail language.\n"
        "- Keep cold outreach compact: usually 50-125 words for the body, opt-out line, and signature combined. Prefer "
        "short paragraphs, plain text first, and one clear low-pressure question.\n"
        "- Use a simple subject under 80 characters. Avoid Re:, Fwd:, fake prior relationship, all caps, repeated punctuation, "
        "emoji, clickbait, curiosity gaps, and excessive personalization in the subject.\n"
        "- Avoid spam-triggering language such as guaranteed, risk-free, act now, limited time, 100%, free!!!, "
        "winner, urgent, no obligation, best price, click here, and similar promotional pressure.\n"
        "- De-market promotional copy by replacing hard-sell, discount-first, urgency-heavy phrasing with neutral, "
        "professional, conversational wording while preserving the truthful commercial intent.\n"
        "- Avoid all-caps emphasis, excessive punctuation, emojis, suspicious formatting, heavy HTML, hidden text, "
        "short links, tracking-heavy links, attachments, and link-heavy copy.\n"
        "- Prefer 0-1 links in early outreach and never use URL shorteners. If a link is not essential, ask for permission "
        "to send it in a reply instead.\n"
        "- Keep an easy opt-out or not-relevant path when the user included one. Preserve required compliance language; "
        "do not hide, obfuscate, or make it misleading.\n"
        "- Keep claims accurate, modest, and supportable. Be especially careful with financial, legal, medical, "
        "security, compliance, and performance claims.\n"
        "- Do not mention spam filters, algorithms, bypassing detection, hidden tricks, or technical evasion in the generated email body.\n"
        "\n"
        "Strict output contract for copy variant generation:\n"
        "- Return only the complete rewritten email body, ready to paste back into the template field.\n"
        "- Do not include markdown fences, explanations, labels, headings, bullet lists, comments, JSON, XML, or a subject line.\n"
        "- Preserve all merge variables exactly, including {Name}, {Company}, {Company_Bio}, {Position}, {AI_Icebreaker}, "
        "custom {Column_Name} variables, and protected tokens like __EPETREL_VAR_0__.\n"
        "- Generate variants only in the fixed copy outside merge variables. Never rewrite, translate, split, duplicate, "
        "remove, or place merge variables inside Spintax options.\n"
        "- Correct: {Hi|Hello} {Name}, I had {a quick thought|a small idea} for {Company}.\n"
        "- Incorrect: {Hi {Name}|Hello {Name}}, {your company|{Company}}, or {__EPETREL_VAR_0__|your team}.\n"
        "- Preserve the original paragraph structure, line breaks, and HTML tags unless a tiny wording change requires otherwise.\n"
        "- Use only single-brace Spintax for variants, for example {quick thought|small idea|brief note}.\n"
        "- Do not nest braces or Spintax. Do not create empty options. Do not use braces for anything except merge variables and Spintax.\n"
        "- Make each Spintax option grammatically interchangeable in the sentence, similar in meaning, and naturally human.\n"
        "- Add variants to meaningful phrases, greetings, transitions, value framing, and soft calls to action; do not vary every word.\n"
        "- Keep the result compact enough for a cold email and safe for automated rendering."
    ),
)

# Deliverability guardrails
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "2"))
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "40"))
REMARKETING_COOLDOWN_DAYS = int(os.getenv("REMARKETING_COOLDOWN_DAYS", "7"))
MAX_DOMAIN_DAILY_SENDS = int(os.getenv("MAX_DOMAIN_DAILY_SENDS", "0"))
DISPATCH_LONG_PAUSE_PROBABILITY = float(os.getenv("DISPATCH_LONG_PAUSE_PROBABILITY", "0"))
DISPATCH_LONG_PAUSE_MIN_SECONDS = int(os.getenv("DISPATCH_LONG_PAUSE_MIN_SECONDS", "900"))
DISPATCH_LONG_PAUSE_MAX_SECONDS = int(os.getenv("DISPATCH_LONG_PAUSE_MAX_SECONDS", "1800"))
DISPATCH_BATCH_BREAK_EVERY = int(os.getenv("DISPATCH_BATCH_BREAK_EVERY", "0"))
DISPATCH_BATCH_BREAK_MIN_SECONDS = int(os.getenv("DISPATCH_BATCH_BREAK_MIN_SECONDS", "600"))
DISPATCH_BATCH_BREAK_MAX_SECONDS = int(os.getenv("DISPATCH_BATCH_BREAK_MAX_SECONDS", "1200"))
BOUNCE_RATE_ALERT = float(os.getenv("BOUNCE_RATE_ALERT", "0.03"))
HARD_BOUNCE_RATE_ALERT = float(os.getenv("HARD_BOUNCE_RATE_ALERT", "0.02"))
UNSUBSCRIBE_RATE_ALERT = float(os.getenv("UNSUBSCRIBE_RATE_ALERT", "0.01"))
SPAM_PLACEMENT_RATE_ALERT = float(os.getenv("SPAM_PLACEMENT_RATE_ALERT", "0.10"))

# ePetrel managed service endpoints. These are product defaults, not user config.
EPETREL_SITE_URL = "https://epetrel.com"
EPETREL_BFF_BASE_URL = "https://bff.epetrel.com"
EMAIL_TEST_POLL_SECONDS = int(os.getenv("EMAIL_TEST_POLL_SECONDS", "90"))
EMAIL_TEST_POLL_INTERVAL_SECONDS = int(os.getenv("EMAIL_TEST_POLL_INTERVAL_SECONDS", "3"))

# MutualWarm local defaults. The authoritative production scheduler lives in WP,
# but the client shows and enforces these defaults before server config arrives.
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
    for item in os.getenv(
        "WARM_ALLOWED_EMAIL_DOMAINS",
        "gmail.com,googlemail.com,outlook.com,hotmail.com,live.com,msn.com,yahoo.com,ymail.com",
    ).split(",")
    if item.strip()
]
