import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from config import (
    WARM_ALLOWED_EMAIL_DOMAINS,
    WARM_AVOID_WEEKENDS,
    WARM_LOCAL_TIMEZONE,
    WARM_REPLY_HARD_TIMEOUT_HOURS,
    WARM_REPLY_MIN_DELAY_HOURS,
    WARM_SCAN_HARD_TIMEOUT_HOURS,
    WARM_SCAN_SOFT_TIMEOUT_HOURS,
    WARM_SLEEP_END_HOUR,
    WARM_SLEEP_START_HOUR,
)


PROVIDER_DOMAINS = {
    "gmail": {"gmail.com", "googlemail.com"},
    "outlook": {"outlook.com", "hotmail.com", "live.com", "msn.com"},
    "yahoo": {"yahoo.com", "ymail.com"},
}

WARM_RULES = [
    "Private Trust Cluster warm-up only matches mailboxes approved by the Cluster Owner.",
    "Cluster Secret stays on local machines and is never uploaded to ePetrel.",
    "New members stay pending until the Owner clicks Allow.",
    "Remove blocks a mailbox from future private warm tasks, even if it still has the old Cluster Secret.",
    "Same public-IP mailboxes trigger a visible risk warning; legitimate internal/team warm can continue.",
    "Private warm tasks are fully free.",
    "ePetrel never uploads mailbox passwords, OAuth refresh tokens, app passwords, or email body content.",
]


def detect_provider(email):
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    for provider, domains in PROVIDER_DOMAINS.items():
        if domain in domains:
            return provider
    return "custom" if domain else ""


def warm_domain_allowed(email):
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    return bool(domain and domain in set(WARM_ALLOWED_EMAIL_DOMAINS))


def warm_policy_config():
    avoid_sleep_hours = WARM_SLEEP_START_HOUR != WARM_SLEEP_END_HOUR
    return {
        "scan_soft_timeout_hours": WARM_SCAN_SOFT_TIMEOUT_HOURS,
        "scan_hard_timeout_hours": WARM_SCAN_HARD_TIMEOUT_HOURS,
        "reply_min_delay_hours": WARM_REPLY_MIN_DELAY_HOURS,
        "reply_hard_timeout_hours": WARM_REPLY_HARD_TIMEOUT_HOURS,
        "sleep_start_hour": WARM_SLEEP_START_HOUR,
        "sleep_end_hour": WARM_SLEEP_END_HOUR,
        "avoid_sleep_hours": avoid_sleep_hours,
        "avoid_weekends": WARM_AVOID_WEEKENDS,
        "timezone": WARM_LOCAL_TIMEZONE,
        "allowed_domains": WARM_ALLOWED_EMAIL_DOMAINS,
    }


def generate_cluster_id():
    return f"wcl_{uuid.uuid4().hex}"


def generate_cluster_secret():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def derive_owner_public_key(cluster_secret):
    secret = (cluster_secret or "").encode("utf-8")
    return hashlib.sha256(b"epetrel-warm-owner:" + secret).hexdigest()


def generate_owner_keypair():
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        base64.urlsafe_b64encode(private_raw).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("="),
    )


def owner_action_message(cluster_id, action, email="", nonce="", timestamp=""):
    return "|".join(
        [
            str(cluster_id or ""),
            str(action or ""),
            str((email or "").strip().lower()),
            str(nonce or ""),
            str(timestamp or ""),
        ]
    )


def sign_owner_action(owner_private_key, cluster_id, action, email="", nonce="", timestamp=""):
    padded = (owner_private_key or "") + "=" * (-len(owner_private_key or "") % 4)
    private_raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_raw)
    signature = private_key.sign(owner_action_message(cluster_id, action, email, nonce, timestamp).encode("utf-8"))
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def make_owner_signature(owner_private_key, cluster_id, action, email=""):
    timestamp = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    nonce = secrets.token_urlsafe(16)
    return {
        "owner_signature": sign_owner_action(owner_private_key, cluster_id, action, email, nonce, timestamp),
        "nonce": nonce,
        "timestamp": timestamp,
    }


def _timezone(name):
    try:
        return ZoneInfo(name or WARM_LOCAL_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _is_sleep_hour(value):
    hour = value.hour
    if WARM_SLEEP_START_HOUR == WARM_SLEEP_END_HOUR:
        return False
    if WARM_SLEEP_START_HOUR < WARM_SLEEP_END_HOUR:
        return WARM_SLEEP_START_HOUR <= hour < WARM_SLEEP_END_HOUR
    return hour >= WARM_SLEEP_START_HOUR or hour < WARM_SLEEP_END_HOUR


def _next_awake_time(value):
    if not _is_sleep_hour(value):
        return value
    if value.hour >= WARM_SLEEP_START_HOUR:
        value = value + timedelta(days=1)
    return value.replace(hour=WARM_SLEEP_END_HOUR, minute=15, second=0, microsecond=0)


def _next_weekday_time(value):
    if not WARM_AVOID_WEEKENDS or value.weekday() < 5:
        return value
    days_until_monday = 7 - value.weekday()
    return (value + timedelta(days=days_until_monday)).replace(hour=9, minute=15, second=0, microsecond=0)


def next_human_reply_time(reference_time=None, timezone_name=None):
    tz = _timezone(timezone_name)
    base = reference_time or datetime.now(tz)
    if base.tzinfo is None:
        base = base.replace(tzinfo=tz)
    candidate = base.astimezone(tz) + timedelta(hours=max(2, WARM_REPLY_MIN_DELAY_HOURS))
    previous = None
    while previous != candidate:
        previous = candidate
        candidate = _next_weekday_time(_next_awake_time(candidate))
    return candidate
