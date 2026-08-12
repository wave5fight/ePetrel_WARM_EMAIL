"""Runtime HTTP(S) proxy settings for the local client.

The UI stores the setting in the local app_settings table.  The environment
variables are updated before requests are made because requests, google-auth,
and the OpenAI SDK all honor the standard proxy environment variables.
"""

import os
from urllib.parse import urlsplit, urlunsplit

from database.db_manager import (
    get_app_setting,
    get_secret_app_setting,
    upsert_app_setting,
    upsert_secret_app_setting,
)


PROXY_ENABLED_SETTING = "network_proxy_enabled"
PROXY_URL_SETTING = "network_proxy_url"
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")
SUPPORTED_PROXY_SCHEMES = {"http", "https"}


def _environment_proxy_url():
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def normalize_proxy_url(value):
    """Return a normalized HTTP(S) proxy URL or raise ValueError."""
    value = (value or "").strip()
    if not value:
        raise ValueError("Proxy address is required when the proxy is enabled.")
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("Proxy address must use http:// or https://.")
    if not parsed.hostname:
        raise ValueError("Proxy address must include a host, for example 127.0.0.1:7890.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Proxy port must be a number between 1 and 65535.") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("Proxy port must be a number between 1 and 65535.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Proxy address must be a host and port, without a path or query string.")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))


def get_proxy_settings():
    saved_enabled = get_app_setting(PROXY_ENABLED_SETTING, None)
    environment_url = _environment_proxy_url()
    saved_url = get_secret_app_setting(PROXY_URL_SETTING, None)
    url = (saved_url if saved_url is not None else environment_url) or ""
    enabled = _parse_bool(saved_enabled, default=bool(environment_url))
    return {
        "enabled": enabled,
        "url": url,
        "source": "saved" if saved_enabled is not None or saved_url is not None else "environment",
    }


def apply_proxy_settings(settings=None):
    """Apply saved proxy settings to this process and return the effective values."""
    settings = settings or get_proxy_settings()
    for key in PROXY_ENV_KEYS + NO_PROXY_KEYS:
        os.environ.pop(key, None)
    if settings.get("enabled"):
        proxy_url = normalize_proxy_url(settings.get("url"))
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    return settings


def save_proxy_settings(enabled, proxy_url):
    normalized_url = ""
    if enabled:
        normalized_url = normalize_proxy_url(proxy_url)
    upsert_app_setting(PROXY_ENABLED_SETTING, "1" if enabled else "0")
    if normalized_url:
        upsert_secret_app_setting(PROXY_URL_SETTING, normalized_url)
    elif proxy_url:
        # Keep a disabled user's last value available if they turn the switch
        # back on later, while still allowing an empty initial configuration.
        upsert_secret_app_setting(PROXY_URL_SETTING, proxy_url.strip())
    settings = {"enabled": bool(enabled), "url": normalized_url or (proxy_url or "").strip(), "source": "saved"}
    apply_proxy_settings(settings)
    return settings
