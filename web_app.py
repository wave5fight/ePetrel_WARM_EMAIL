import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import (
    DB_PATH,
    DEFAULT_DAILY_LIMIT,
    EPETREL_SITE_URL,
    OPENAI_BASE_URL,
    WARM_MAILBOX_OFFLINE_WARN_SEC,
    WARM_MAILBOX_STALE_SEC,
    WARM_PROBE_REPLY_MAX_DELAY_SECONDS,
    WARM_PROBE_REPLY_MIN_DELAY_SECONDS,
    WARM_PROBE_RESCAN_TIMEOUT_SECONDS,
    WARM_PROBE_SCAN_MAX_INTERVAL_SECONDS,
    WARM_PROBE_SCAN_MIN_INTERVAL_SECONDS,
    WARM_PROBE_SCAN_TIMEOUT_SECONDS,
    WARM_WORKER_ENABLED,
)
from database.db_manager import (
    WARM_LLM_SYSTEM_PROMPT,
    clear_senders,
    clear_warm_cluster_state,
    clear_warm_mailboxes,
    delete_sender,
    delete_warm_cluster_member,
    delete_warm_mailbox,
    get_llm_settings,
    get_secret_app_setting,
    get_sender,
    get_warm_cluster,
    get_warm_summary,
    init_db,
    keep_only_warm_cluster,
    list_senders,
    list_warm_cluster_members,
    list_warm_clusters,
    list_warm_mailboxes,
    log_warm_event,
    mark_warm_cluster_dissolved,
    upsert_llm_settings,
    upsert_secret_app_setting,
    upsert_sender,
    upsert_warm_cluster,
    upsert_warm_cluster_member,
    upsert_warm_mailbox,
    update_warm_cluster_member_status,
    update_warm_mailbox_status,
)
from modules.email_utils import get_domain, normalize_email
from modules.gmail_api import (
    GMAIL_FULL_AUTO_WARM_SCOPES,
    GMAIL_MODIFY_SCOPE,
    build_gmail_oauth_url,
    exchange_gmail_oauth_code,
    fetch_gmail_profile,
)
from modules.network_proxy import apply_proxy_settings, get_proxy_settings, save_proxy_settings
from modules.safe_logging import configure_file_logger, mask_email, redact_sensitive
from modules.warm_account_probe import (
    GMAIL_MODIFY_SETUP_HINT,
    move_warm_account_probe_to_inbox,
    scan_warm_account_probe,
    send_warm_account_probe_reply,
    warm_inbox_rescue_capability,
)
from modules.warm_client import (
    WARM_RULES,
    derive_owner_public_key,
    detect_provider,
    generate_cluster_id,
    generate_cluster_secret,
    generate_owner_keypair,
    make_owner_signature,
    next_human_reply_time,
    warm_policy_config,
)
from modules.warm_content import WARM_CONTENT_STAGES, WARM_TOPICS, generate_warm_content, warm_llm_self_check
from modules.warm_service import (
    WarmApiError,
    approve_warm_cluster_member,
    create_warm_cluster,
    dissolve_warm_cluster,
    fetch_warm_cluster_members,
    fetch_warm_summary,
    join_warm_cluster,
    leave_warm_cluster,
    list_warm_mailbox_ownership,
    poll_warm_auth,
    register_warm_mailbox,
    remove_warm_cluster_member,
    report_warm_mailbox_ownership_reply,
    start_warm_auth,
    start_warm_mailbox_ownership,
    verify_warm_mailbox_ownership,
)
from modules.warm_worker import set_warm_worker_auth, start_warm_worker, stop_warm_worker


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WARM_AUTH_SETTING_KEY = "warm_auth_json"
AUTH_CACHE_TTL_SECONDS = 10 * 60
WARM_AUTH_CACHE = {}
GMAIL_OAUTH_PENDING = {}
GMAIL_ACCOUNT_TYPES = {"consumer_gmail", "workspace_gmail"}
GMAIL_ACCOUNT_TYPE_DAILY_LIMITS = {"consumer_gmail": 20, "workspace_gmail": 40}
PAGE_KEYS = ["warm", "config"]
LANGUAGE_LABELS = {"en": "English", "zh": "中文"}

WARM_RULE_CARD_META = [
    {"icon": "lock", "tone": "primary"},
    {"icon": "shield_person", "tone": "success"},
    {"icon": "person_check", "tone": "primary"},
    {"icon": "link_off", "tone": "danger"},
    {"icon": "warning", "tone": "warning"},
    {"icon": "card_giftcard", "tone": "success"},
    {"icon": "visibility_off", "tone": "muted", "wide": True},
]

TEXT = {
    "en": {
        "app_name": "MutualWarm",
        "app_subtitle": "Open Source Warm Client",
        "system_status": "System Operational",
        "page.warm": "Warm Network",
        "page.config": "Configuration",
        "warm_title": "MutualWarm Network",
        "warm_caption": "Decentralized inbox placement, delayed replies, and contribution tracking for opted-in Gmail and Google Workspace mailboxes.",
        "config_title": "Configuration",
        "config_caption": "Connect Gmail / Google Workspace mailboxes, configure Gmail OAuth, and save the Warm OpenAI-compatible LLM used by MutualWarm.",
        "sender_pool": "Warm Mailbox Pool",
        "available_senders": "{count} Gmail API mailbox(es) saved locally.",
        "clear_senders": "Clear Mailboxes",
        "clear_senders_confirm": "Clear all saved Gmail API mailboxes from this local client?",
        "delete_sender": "Delete mailbox",
        "delete_sender_confirm": "Delete this saved mailbox?",
        "deleted_sender": "Deleted mailbox {email}.",
        "delete_sender_missing": "Mailbox was not found.",
        "cleared_senders": "Cleared {count} saved mailbox(es).",
        "no_senders": "No Gmail API mailbox has been configured.",
        "sender_email": "Gmail / Workspace email",
        "gmail_account_type": "Gmail account type",
        "gmail_account_type_consumer": "Personal Gmail",
        "gmail_account_type_workspace": "Google Workspace",
        "daily_limit": "Daily Warm Limit",
        "from_name": "From Name",
        "gmail_client_id": "Gmail OAuth Client ID",
        "gmail_client_secret": "Gmail OAuth Client Secret",
        "gmail_modify_scope": "Full Auto Warm Gmail read/rescue scopes",
        "gmail_modify_scope_hint": "MutualWarm requests Gmail send, readonly, and modify scopes so it can send warm messages, scan placement, move supported spam placements to Inbox, and reply to ownership probes.",
        "gmail_api_hint": "Only Gmail API / Google Workspace OAuth is supported in this standalone MutualWarm client.",
        "gmail_sender_oauth_hint": "Save a mailbox locally or connect it directly with Gmail API OAuth. Client Secret is encrypted in local SQLite settings.",
        "gmail_oauth_link": "Gmail OAuth link",
        "save_sender": "Save Mailbox",
        "connect_gmail_api": "Connect Gmail API",
        "copy_gmail_oauth_link": "Copy OAuth Link",
        "progress_sender_save_title": "Saving mailbox",
        "progress_sender_save_text": "Saving Gmail API mailbox settings locally.",
        "progress_gmail_oauth_title": "Starting Gmail OAuth",
        "progress_gmail_oauth_text": "Opening Google authorization for this mailbox.",
        "auth_method": "Auth Method",
        "auth_method_gmail_api": "Gmail API",
        "gmail_api_missing_config": "Enter a valid Gmail address, From Name, daily limit, Gmail OAuth Client ID, and Client Secret. If a Client Secret is saved locally, you can leave it blank when reconnecting.",
        "gmail_api_saved": "Saved Gmail API mailbox {email}. Connect OAuth before enabling Warm.",
        "gmail_api_connected": "Gmail API connected for {email}.",
        "gmail_api_connected_limited": "Gmail API connected for {email}, but some requested scopes were not granted.",
        "gmail_api_failed": "Gmail API failed: {error}",
        "gmail_oauth_email_mismatch": "Google authorized {actual}, but this form expected {expected}.",
        "gmail_oauth_link_ready": "OAuth link ready for {email}.",
        "mailbox_check": "Warm Readiness",
        "warm_enabled": "Warm mailbox enabled: {email}.",
        "warm_paused": "Warm mailbox status updated: {email}.",
        "warm_sender_required": "Choose a saved Gmail API mailbox before enabling Warm Network.",
        "warm_current_llm": "Warm LLM Configuration",
        "warm_llm_caption": "Use an OpenAI-compatible model for short, natural warm mailbox conversations.",
        "api_key": "API Key",
        "base_url": "Base URL",
        "model": "Model",
        "system_prompt": "System Prompt",
        "llm_missing_key": "The Warm LLM provider has no saved API key yet.",
        "save_warm_llm": "Save Warm LLM",
        "llm_saved": "Warm LLM settings saved.",
        "progress_llm_save_title": "Saving Warm LLM",
        "progress_llm_save_text": "Encrypting the provider key and updating local Warm model settings.",
        "proxy_settings": "Proxy Settings",
        "proxy_settings_caption": "Optional outbound HTTP(S) proxy used by ePetrel, Google, and model API requests.",
        "proxy_enabled": "Enable proxy",
        "proxy_enabled_hint": "When off, this client does not apply the proxy configured here.",
        "proxy_address": "Proxy URL",
        "proxy_address_placeholder": "http://127.0.0.1:7890",
        "proxy_note_title": "Local-only setting",
        "proxy_note": "The proxy URL is stored on this machine and is applied before outbound network calls.",
        "proxy_scope_note": "Use this when your network needs a local proxy for Google OAuth, Gmail API, ePetrel, or LLM requests.",
        "proxy_save": "Save Proxy",
        "proxy_saved": "Proxy settings saved.",
        "proxy_invalid": "Proxy settings were not saved: {error}",
    },
    "zh": {},
}
TEXT["zh"] = TEXT["en"]

app_logger = configure_file_logger("epetrel.web", os.path.join(BASE_DIR, "logs", "web.log"))
gmail_oauth_logger = configure_file_logger("epetrel.gmail_oauth", os.path.join(BASE_DIR, "logs", "gmail_oauth.log"))

init_db()
apply_proxy_settings()

app = FastAPI(title="MutualWarm", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("EPETREL_SESSION_SECRET", "epetrel-local-session-dev"))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def startup_warm_worker():
    if WARM_WORKER_ENABLED:
        cached_auth = load_persisted_warm_auth()
        if cached_auth.get("access_token"):
            set_warm_worker_auth(cached_auth)
        start_warm_worker()


@app.on_event("shutdown")
async def shutdown_warm_worker():
    await stop_warm_worker()


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0043ae"/>
<path d="M18 21h28v6H25v7h17v6H25v13h-7z" fill="#fff"/>
<circle cx="47" cy="17" r="5" fill="#dbe1ff"/>
</svg>"""


def auth_cache_set(store, key, value, ttl_seconds=AUTH_CACHE_TTL_SECONDS):
    if key:
        store[str(key)] = {"expires_at": time.time() + max(60, int(ttl_seconds)), "value": value}


def auth_cache_get(store, key, default=None):
    if not key:
        return default
    record = store.get(str(key))
    if not record:
        return default
    if float(record.get("expires_at") or 0) < time.time():
        store.pop(str(key), None)
        return default
    return record.get("value", default)


def warm_auth_is_authorized(auth_data):
    status = str((auth_data or {}).get("status") or "").lower()
    return bool((auth_data or {}).get("access_token")) or status == "authorized"


def provider_label(provider):
    return "OpenAI / Compatible"



def t(lang, key, **kwargs):
    value = TEXT.get(lang, TEXT["en"]).get(key, TEXT["en"].get(key, key))
    return value.format(**kwargs) if kwargs else value


async def unhandled_exception_handler(request: Request, exc: Exception):
    app_logger.exception(
        "unhandled request error method=%s path=%s error=%s",
        request.method,
        request.url.path,
        redact_sensitive(str(exc)),
    )
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"error": "Internal server error"}, status_code=500)
    return HTMLResponse("Internal server error", status_code=500)


def warm_auth_expires_at(auth_data):
    try:
        explicit = float((auth_data or {}).get("_expires_at") or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit:
        return explicit
    try:
        expires_in = int((auth_data or {}).get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    try:
        stored_at = float((auth_data or {}).get("_stored_at") or 0)
    except (TypeError, ValueError):
        stored_at = 0
    return stored_at + expires_in if stored_at and expires_in else 0


def warm_auth_is_expired(auth_data):
    expires_at = warm_auth_expires_at(auth_data)
    return bool(expires_at and expires_at <= time.time() + 30)


def persist_warm_auth(auth_data):
    if not warm_auth_is_authorized(auth_data):
        return
    data = dict(auth_data or {})
    data["_stored_at"] = time.time()
    try:
        expires_in = int(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in:
        data["_expires_at"] = time.time() + max(0, expires_in)
    upsert_secret_app_setting(WARM_AUTH_SETTING_KEY, json.dumps(data, ensure_ascii=True))


def clear_persisted_warm_auth():
    upsert_secret_app_setting(WARM_AUTH_SETTING_KEY, "")


def load_persisted_warm_auth():
    raw = get_secret_app_setting(WARM_AUTH_SETTING_KEY, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        clear_persisted_warm_auth()
        return {}
    if not warm_auth_is_authorized(data) or warm_auth_is_expired(data):
        clear_persisted_warm_auth()
        return {}
    return data


def _session_epetrel_auth(request, key):
    auth_data = request.session.get(key) or {}
    if key == "warm_auth" and (not warm_auth_is_authorized(auth_data) or warm_auth_is_expired(auth_data)):
        persisted_auth = load_persisted_warm_auth()
        if warm_auth_is_authorized(persisted_auth):
            auth_data = persisted_auth
            request.session[key] = auth_data
    if warm_auth_is_authorized(auth_data):
        if key == "warm_auth":
            set_warm_worker_auth(auth_data)
        return auth_data
    return {}


def _store_epetrel_auth(request, key, auth_data, device_code=""):
    request.session[key] = auth_data
    if device_code:
        auth_cache_set(WARM_AUTH_CACHE, device_code, auth_data, ttl_seconds=10 * 60)


def _warm_store_auth(request, auth_data, device_code=""):
    _store_epetrel_auth(request, "warm_auth", auth_data, device_code)
    persist_warm_auth(auth_data)
    set_warm_worker_auth(auth_data)


def _warm_auth_error_is_invalid_token(exc):
    message = str(exc).lower()
    return "invalid warm token" in message or "warm token expired" in message


def _warm_error_is_cluster_dissolved(exc):
    message = str(exc).lower()
    return "cluster_dissolved" in message or "warm cluster has been dissolved" in message or "warm 群已被群主解散" in message


def _clear_warm_auth(request):
    request.session["warm_auth"] = {}
    request.session["warm_auth_request"] = {}
    request.session["warm_auth_started_at"] = 0
    clear_persisted_warm_auth()
    set_warm_worker_auth({})


def normalize_gmail_account_type(value, email=""):
    value = (value or "").strip().lower()
    if value in GMAIL_ACCOUNT_TYPES:
        return value
    domain = get_domain(normalize_email(email))
    return "consumer_gmail" if domain in {"gmail.com", "googlemail.com"} else "workspace_gmail"


def gmail_account_default_daily_limit(account_type):
    return GMAIL_ACCOUNT_TYPE_DAILY_LIMITS.get(account_type, DEFAULT_DAILY_LIMIT)


def flash(request, level, message):
    messages = request.session.setdefault("flash", [])
    messages.append({"level": level, "message": message})


def redirect(path):
    return RedirectResponse(path, status_code=303)


def local_redirect_target(path, default="/"):
    path = (path or "").strip()
    if path.startswith("/") and not path.startswith("//"):
        return path
    return default


def get_lang(request):
    request.session["language"] = "en"
    return "en"


def page_context(request, page, title_key, caption_key, **extra):
    lang = get_lang(request)
    context = {
        "request": request,
        "page": page,
        "lang": lang,
        "language_labels": LANGUAGE_LABELS,
        "nav": PAGE_KEYS,
        "title": t(lang, title_key),
        "caption": t(lang, caption_key),
        "flash": request.session.pop("flash", []),
        "t": lambda key, **kwargs: t(lang, key, **kwargs),
        "provider_label": provider_label,
    }
    context.update(extra)
    return context



@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return await warm_page(request)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(FAVICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/settings/proxy")
async def save_proxy_route(request: Request, proxy_enabled: str = Form(""), proxy_url: str = Form("")):
    lang = get_lang(request)
    enabled = proxy_enabled.strip().lower() in {"1", "true", "on", "yes"}
    try:
        save_proxy_settings(enabled, proxy_url)
    except ValueError as exc:
        flash(request, "error", t(lang, "proxy_invalid", error=str(exc)))
        return redirect("/config")
    flash(request, "success", t(lang, "proxy_saved"))
    return redirect("/config")


@app.post("/senders")
async def save_sender(
    request: Request,
    sender_email: str = Form(""),
    gmail_account_type: str = Form("workspace_gmail"),
    daily_limit: int = Form(DEFAULT_DAILY_LIMIT),
    from_name: str = Form("MutualWarm"),
    gmail_client_id: str = Form(""),
    gmail_client_secret: str = Form(""),
):
    lang = get_lang(request)
    normalized = normalize_email(sender_email)
    account_type = normalize_gmail_account_type(gmail_account_type, normalized)
    existing = get_sender(normalized) if normalized else None
    client_id = (gmail_client_id or "").strip() or (existing or {}).get("gmail_client_id", "")
    client_secret = (gmail_client_secret or "").strip() or (existing or {}).get("gmail_client_secret", "")
    if not normalized or not from_name.strip() or int(daily_limit or 0) <= 0 or not client_id or not client_secret:
        flash(request, "error", t(lang, "gmail_api_missing_config"))
        return redirect("/config")
    if account_type == "consumer_gmail" and get_domain(normalized) not in {"gmail.com", "googlemail.com"}:
        flash(request, "error", t(lang, "gmail_api_missing_config"))
        return redirect("/config")
    upsert_sender(
        normalized,
        daily_limit=int(daily_limit),
        from_name=from_name.strip(),
        auth_method="gmail_api",
        gmail_client_id=client_id,
        gmail_client_secret=client_secret if gmail_client_secret else None,
        gmail_refresh_token=None,
        gmail_token_status=(existing or {}).get("gmail_token_status") or "not_connected",
        gmail_granted_scopes=(existing or {}).get("gmail_granted_scopes", ""),
        gmail_account_type=account_type,
        mailbox_check_status="api_configured" if not (existing or {}).get("gmail_refresh_token") else "passed",
        check_error="",
    )
    flash(request, "success", t(lang, "gmail_api_saved", email=normalized))
    return redirect("/config")


@app.post("/gmail/oauth/start")
async def gmail_oauth_start(
    request: Request,
    sender_email: str = Form(""),
    daily_limit: int = Form(DEFAULT_DAILY_LIMIT),
    from_name: str = Form("MutualWarm"),
    gmail_account_type: str = Form("workspace_gmail"),
    gmail_client_id: str = Form(""),
    gmail_client_secret: str = Form(""),
    oauth_action: str = Form("redirect"),
):
    lang = get_lang(request)
    normalized = normalize_email(sender_email)
    existing = get_sender(normalized) if normalized else None
    account_type = normalize_gmail_account_type(gmail_account_type, normalized)
    client_id = (gmail_client_id or "").strip() or (existing or {}).get("gmail_client_id", "")
    client_secret = (gmail_client_secret or "").strip() or (existing or {}).get("gmail_client_secret", "")
    if not normalized or not from_name.strip() or int(daily_limit or 0) <= 0 or not client_id or not client_secret:
        flash(request, "error", t(lang, "gmail_api_missing_config"))
        return redirect("/config")
    if account_type == "consumer_gmail" and get_domain(normalized) not in {"gmail.com", "googlemail.com"}:
        flash(request, "error", t(lang, "gmail_api_missing_config"))
        return redirect("/config")

    normalized_daily_limit = int(daily_limit)
    if account_type == "consumer_gmail" and normalized_daily_limit == int(DEFAULT_DAILY_LIMIT):
        normalized_daily_limit = gmail_account_default_daily_limit(account_type)

    state = f"gmail_{uuid.uuid4()}"
    redirect_uri = str(request.url_for("gmail_oauth_callback"))
    GMAIL_OAUTH_PENDING[state] = {
        "expires_at": time.time() + 10 * 60,
        "email": normalized,
        "daily_limit": normalized_daily_limit,
        "from_name": from_name.strip(),
        "gmail_account_type": account_type,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "requested_scopes": list(GMAIL_FULL_AUTO_WARM_SCOPES),
    }
    try:
        authorization_url, code_verifier = build_gmail_oauth_url(
            client_id,
            client_secret,
            redirect_uri,
            state,
            login_hint=normalized,
            scopes=GMAIL_FULL_AUTO_WARM_SCOPES,
        )
        GMAIL_OAUTH_PENDING[state]["code_verifier"] = code_verifier
        gmail_oauth_logger.info("gmail oauth start email=%s state=%s", mask_email(normalized), state[:18])
    except Exception as exc:
        GMAIL_OAUTH_PENDING.pop(state, None)
        gmail_oauth_logger.exception("gmail oauth start failed email=%s state=%s error=%s", mask_email(normalized), state[:18], redact_sensitive(str(exc)))
        flash(request, "error", t(lang, "gmail_api_failed", error=str(exc)))
        return redirect("/config")
    if (oauth_action or "").strip().lower() == "copy":
        request.session["gmail_oauth_authorization_url"] = authorization_url
        flash(request, "success", t(lang, "gmail_oauth_link_ready", email=normalized))
        return redirect("/config")
    return RedirectResponse(authorization_url, status_code=303)


@app.get("/gmail/oauth/callback")
async def gmail_oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    lang = get_lang(request)
    pending = GMAIL_OAUTH_PENDING.pop(state, None)
    if error:
        gmail_oauth_logger.warning("gmail oauth callback returned error state=%s error=%s", state[:18], error)
        flash(request, "error", t(lang, "gmail_api_failed", error=error))
        return redirect("/config")
    if not pending or float(pending.get("expires_at") or 0) < time.time():
        gmail_oauth_logger.warning("gmail oauth callback expired or missing state=%s", state[:18])
        flash(request, "error", t(lang, "gmail_api_failed", error="OAuth request expired. Start again."))
        return redirect("/config")
    if not code:
        flash(request, "error", t(lang, "gmail_api_failed", error="Missing OAuth code."))
        return redirect("/config")

    try:
        credentials = exchange_gmail_oauth_code(
            pending["client_id"],
            pending["client_secret"],
            pending["redirect_uri"],
            str(request.url),
            state,
            code_verifier=pending.get("code_verifier"),
            scopes=pending.get("requested_scopes") or GMAIL_FULL_AUTO_WARM_SCOPES,
        )
        refresh_token = credentials.refresh_token
        if not refresh_token:
            raise RuntimeError("Google did not return a refresh token. Revoke the app grant in your Google account, then reconnect.")
        granted_scopes = sorted(set(credentials.granted_scopes or credentials.scopes or pending.get("requested_scopes") or GMAIL_FULL_AUTO_WARM_SCOPES))
        profile = fetch_gmail_profile(credentials.token)
        authorized_email = normalize_email(profile.get("emailAddress", ""))
        expected_email = normalize_email(pending["email"])
        if not authorized_email or authorized_email != expected_email:
            flash(request, "error", t(lang, "gmail_oauth_email_mismatch", actual=authorized_email or "unknown", expected=expected_email))
            return redirect("/config")

        upsert_sender(
            pending["email"],
            daily_limit=pending["daily_limit"],
            from_name=pending["from_name"],
            auth_method="gmail_api",
            gmail_client_id=pending["client_id"],
            gmail_client_secret=pending["client_secret"],
            gmail_refresh_token=refresh_token,
            gmail_token_status="connected",
            gmail_granted_scopes=" ".join(granted_scopes),
            gmail_account_type=pending.get("gmail_account_type") or normalize_gmail_account_type("", pending["email"]),
            mailbox_check_status="passed",
            check_error="",
        )
        gmail_oauth_logger.info("gmail oauth connected email=%s state=%s", mask_email(pending["email"]), state[:18])
        if GMAIL_MODIFY_SCOPE in set(pending.get("requested_scopes") or []) and GMAIL_MODIFY_SCOPE not in granted_scopes:
            flash(request, "warning", t(lang, "gmail_api_connected_limited", email=pending["email"]))
        else:
            flash(request, "success", t(lang, "gmail_api_connected", email=pending["email"]))
    except Exception as exc:
        gmail_oauth_logger.exception("gmail oauth callback failed email=%s state=%s error=%s", mask_email((pending or {}).get("email", "")), state[:18], redact_sensitive(str(exc)))
        flash(request, "error", t(lang, "gmail_api_failed", error=str(exc)))
    return redirect("/config")


@app.post("/senders/delete")
async def remove_sender(request: Request, sender_email: str = Form("")):
    lang = get_lang(request)
    normalized = normalize_email(sender_email)
    if normalized and delete_sender(normalized):
        flash(request, "success", t(lang, "deleted_sender", email=normalized))
    else:
        flash(request, "warning", t(lang, "delete_sender_missing"))
    return redirect("/config")


@app.post("/senders/clear")
async def clear_senders_route(request: Request, next_url: str = Form("/config")):
    lang = get_lang(request)
    deleted = clear_senders()
    flash(request, "success", t(lang, "cleared_senders", count=deleted))
    return redirect(local_redirect_target(next_url, "/config"))



@app.get("/warm", response_class=HTMLResponse)
async def warm_page(request: Request):
    policy = warm_policy_config()
    next_reply = next_human_reply_time(timezone_name=policy["timezone"])
    warm_rule_cards = [
        {**WARM_RULE_CARD_META[index], "text": rule}
        for index, rule in enumerate(WARM_RULES)
        if index < len(WARM_RULE_CARD_META)
    ]
    clusters = list_warm_clusters(include_secrets=False)
    selected_cluster_id = (request.query_params.get("cluster_id") or request.session.get("warm_cluster_id") or "").strip()
    known_cluster_ids = {cluster["cluster_id"] for cluster in clusters}
    if selected_cluster_id and selected_cluster_id not in known_cluster_ids:
        selected_cluster_id = ""
    if not selected_cluster_id and clusters:
        selected_cluster_id = clusters[0]["cluster_id"]
    if selected_cluster_id:
        request.session["warm_cluster_id"] = selected_cluster_id
    clusters = [cluster for cluster in clusters if cluster["cluster_id"] == selected_cluster_id][:1]
    selected_cluster = get_warm_cluster(selected_cluster_id, include_secrets=False) if selected_cluster_id else {}
    if selected_cluster.get("role") == "owner":
        selected_cluster = get_warm_cluster(selected_cluster_id, include_secrets=True)
    warm_auth = _session_epetrel_auth(request, "warm_auth")
    if selected_cluster_id and warm_auth.get("access_token"):
        try:
            sync_remote_warm_cluster_state(warm_auth, selected_cluster_id)
            local_cluster_with_secrets = get_warm_cluster(selected_cluster_id, include_secrets=True)
            selected_cluster = local_cluster_with_secrets if local_cluster_with_secrets.get("cluster_secret") else get_warm_cluster(selected_cluster_id)
        except WarmApiError as exc:
            if _warm_auth_error_is_invalid_token(exc):
                _clear_warm_auth(request)
                warm_auth = {}
            elif _warm_error_is_cluster_dissolved(exc):
                mark_warm_cluster_dissolved(selected_cluster_id)
                selected_cluster = get_warm_cluster(selected_cluster_id)
                flash(request, "warning", "This warm cluster has been dissolved by the owner. Local warm mailboxes were paused. 该 Warm 群已被群主解散，本地 Warm 邮箱已暂停。")
    cluster_members = list_warm_cluster_members(selected_cluster_id) if selected_cluster_id else []
    ownership_mailboxes = []
    if warm_auth.get("access_token"):
        try:
            ownership_mailboxes = list_warm_mailbox_ownership(warm_auth["access_token"]).get("mailboxes", [])
        except WarmApiError as exc:
            if _warm_auth_error_is_invalid_token(exc):
                _clear_warm_auth(request)
                warm_auth = {}
            ownership_mailboxes = []
    warm_summary = get_warm_summary(days=30, cluster_id=selected_cluster_id)
    remote_warm_summary = {}
    remote_warm_summary_error = ""
    if selected_cluster_id and warm_auth.get("access_token"):
        try:
            owner_payload = {}
            if selected_cluster.get("owner_private_key"):
                owner_payload = make_owner_signature(selected_cluster["owner_private_key"], selected_cluster_id, "members_read")
            remote_warm_summary = fetch_warm_summary(
                warm_auth["access_token"],
                cluster_id=selected_cluster_id,
                days=30,
                owner_payload=owner_payload,
            )
        except WarmApiError as exc:
            if _warm_auth_error_is_invalid_token(exc):
                _clear_warm_auth(request)
                warm_auth = {}
            elif _warm_error_is_cluster_dissolved(exc):
                mark_warm_cluster_dissolved(selected_cluster_id)
                remote_warm_summary_error = "This warm cluster has been dissolved by the owner. 该 Warm 群已被群主解散。"
            else:
                remote_warm_summary_error = str(exc)
    warm_summary = prepare_warm_summary_for_display(merge_warm_summary_for_display(warm_summary, remote_warm_summary), selected_cluster)
    senders = list_senders()
    warm_mailboxes = list_warm_mailboxes()
    warm_sender_options = build_warm_sender_options(senders, warm_mailboxes)
    return templates.TemplateResponse(
        request=request,
        name="warm.html",
        context=page_context(
            request,
            "warm",
            "warm_title",
            "warm_caption",
            senders=senders,
            warm_sender_options=warm_sender_options,
            warm_clusters=clusters,
            selected_cluster=selected_cluster,
            warm_cluster_members=cluster_members,
            warm_mailboxes=warm_mailboxes,
            warm_summary=warm_summary,
            remote_warm_summary=remote_warm_summary,
            remote_warm_summary_error=remote_warm_summary_error,
            warm_rules=WARM_RULES,
            warm_rule_cards=warm_rule_cards,
            warm_policy=policy,
            next_reply_preview=next_reply.strftime("%Y-%m-%d %H:%M %Z"),
            warm_auth_request=request.session.get("warm_auth_request") or {},
            warm_auth=warm_auth,
            warm_ownership_mailboxes=ownership_mailboxes,
            warm_ownership_results=request.session.get("warm_ownership_results") or [],
            warm_account_probe=request.session.get("warm_account_probe") or {},
            warm_content_preview=request.session.get("warm_content_preview") or {},
            warm_auto_content_status="local LLM with template fallback",
            warm_content_stages=WARM_CONTENT_STAGES,
            warm_content_topics=WARM_TOPICS,
        ),
    )


@app.get("/warm/summary/status")
async def warm_summary_status(request: Request, cluster_id: str = ""):
    auth_data = _session_epetrel_auth(request, "warm_auth")
    if not auth_data.get("access_token"):
        return JSONResponse({"success": False, "error": "warm_auth_required"}, status_code=401)
    cluster_id = (cluster_id or request.session.get("warm_cluster_id") or "").strip()
    if not cluster_id:
        return JSONResponse({"success": False, "error": "cluster_id_required"}, status_code=400)
    selected_cluster = get_warm_cluster(cluster_id, include_secrets=True)
    if not selected_cluster:
        return JSONResponse({"success": False, "error": "cluster_not_found"}, status_code=404)
    local_summary = get_warm_summary(days=30, cluster_id=cluster_id)
    remote_summary = {}
    remote_error = ""
    try:
        owner_payload = {}
        if selected_cluster.get("owner_private_key"):
            owner_payload = make_owner_signature(selected_cluster["owner_private_key"], cluster_id, "members_read")
        remote_summary = fetch_warm_summary(
            auth_data["access_token"],
            cluster_id=cluster_id,
            days=30,
            owner_payload=owner_payload,
        )
    except WarmApiError as exc:
        if _warm_auth_error_is_invalid_token(exc):
            return JSONResponse({"success": False, "error": "warm_auth_invalid"}, status_code=401)
        remote_error = str(exc)
    summary = prepare_warm_summary_for_display(merge_warm_summary_for_display(local_summary, remote_summary), selected_cluster)
    return JSONResponse({
        "success": True,
        "summary": summary,
        "remote_error": remote_error,
        "cluster_id": cluster_id,
    })


@app.post("/warm/auth/start")
async def warm_auth_start(request: Request):
    try:
        auth_data = _session_epetrel_auth(request, "warm_auth")
        wants_json = (
            request.headers.get("x-requested-with") == "fetch"
            or "application/json" in request.headers.get("accept", "")
        )
        if warm_auth_is_authorized(auth_data):
            if wants_json:
                return JSONResponse({"status": "authorized", "device_code": ""})
            flash(request, "success", "Logged in to ePetrel.")
            return redirect("/warm")
        auth_request = start_warm_auth()
        request.session["warm_auth_request"] = auth_request
        request.session["warm_auth_started_at"] = time.time()
        if warm_auth_is_authorized(auth_request):
            _warm_store_auth(request, auth_request, auth_request.get("device_code", ""))
        if wants_json:
            if warm_auth_is_authorized(auth_request):
                return JSONResponse({"status": "authorized", "device_code": auth_request.get("device_code", "")})
            return JSONResponse(
                {
                    "status": "started",
                    "login_url": auth_request.get("login_url", ""),
                    "device_code": auth_request.get("device_code", ""),
                }
            )
        return redirect(auth_request.get("login_url") or "/warm")
    except WarmApiError as exc:
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=502)
        flash(request, "error", f"Warm auth failed: {exc}")
        return redirect("/warm")


@app.get("/warm/auth/status")
async def warm_auth_status(request: Request):
    auth_data = _session_epetrel_auth(request, "warm_auth")
    if warm_auth_is_authorized(auth_data):
        return JSONResponse({"status": "authorized"})

    auth_request = request.session.get("warm_auth_request") or {}
    device_code = request.query_params.get("device_code") or auth_request.get("device_code", "")
    if not device_code:
        return JSONResponse({"status": "not_started"})

    cached_auth = auth_cache_get(WARM_AUTH_CACHE, device_code)
    if warm_auth_is_authorized(cached_auth):
        _warm_store_auth(request, cached_auth, device_code)
        return JSONResponse({"status": "authorized"})

    try:
        auth = poll_warm_auth(device_code)
        if warm_auth_is_authorized(auth):
            _warm_store_auth(request, auth, device_code)
            return JSONResponse({"status": "authorized"})
        return JSONResponse({"status": "pending"})
    except WarmApiError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=502)


@app.post("/warm/auth/check")
async def warm_auth_check(request: Request):
    if warm_auth_is_authorized(_session_epetrel_auth(request, "warm_auth")):
        flash(request, "success", "Logged in to ePetrel.")
        return redirect("/warm")
    auth_request = request.session.get("warm_auth_request") or {}
    device_code = auth_request.get("device_code", "")
    if not device_code:
        flash(request, "warning", "Start Warm Network authorization first.")
        return redirect("/warm")
    try:
        auth = poll_warm_auth(device_code)
        if auth.get("status") == "authorized" and auth.get("access_token"):
            _warm_store_auth(request, auth, device_code)
            flash(request, "success", "Warm Network authorization completed.")
        else:
            flash(request, "warning", "Warm Network authorization is still pending.")
    except WarmApiError as exc:
        flash(request, "error", f"Warm auth check failed: {exc}")
    return redirect("/warm")


@app.post("/warm/auth/clear")
async def warm_auth_clear(request: Request):
    _clear_warm_auth(request)
    flash(request, "success", "Cleared local ePetrel Warm authorization. Remote mailbox ownership bindings were not deleted.")
    return redirect("/warm")


def get_required_warm_auth(request):
    auth_data = _session_epetrel_auth(request, "warm_auth")
    if auth_data.get("access_token"):
        return auth_data
    flash(request, "error", "Log in to ePetrel from this open-source client before using Warm Network.")
    return None


async def ensure_warm_llm_ready(request, action_label="starting Warm Network"):
    result = await asyncio.to_thread(warm_llm_self_check)
    if result.get("ok"):
        request.session["warm_llm_self_check"] = {
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "subject": result.get("subject", ""),
            "body_preview": result.get("body_preview", ""),
        }
        return True
    flash(
        request,
        "error",
        (
            f"Warm LLM self-check failed before {action_label}: "
            f"{result.get('error') or 'No valid warm LLM content was generated.'} "
            "Open Configuration, set the Warm OpenAI-compatible provider, then try again."
        ),
    )
    return False


def store_warm_account_probe_scan(request, probe, result):
    updated_probe = {**probe, "scan": result, "placement": result.get("placement", ""), "scan_status": result.get("status", "")}
    request.session["warm_account_probe"] = updated_probe
    log_warm_event(
        mailbox_email=probe.get("to_email", "") or probe.get("mailbox_email", ""),
        task_id=probe.get("probe_id", ""),
        event_type="account_probe_placement",
        status=result.get("status", ""),
        placement=result.get("placement", ""),
        message_id=result.get("message_id", ""),
        details=json.dumps(result, ensure_ascii=True),
    )
    return updated_probe


async def scan_warm_account_probe_automatically(mailbox_email, token, subject="", timeout_seconds=None, interval_seconds=None):
    timeout = int(timeout_seconds or WARM_PROBE_SCAN_TIMEOUT_SECONDS or 90)
    min_interval = int(interval_seconds or WARM_PROBE_SCAN_MIN_INTERVAL_SECONDS or 7)
    max_interval = int(WARM_PROBE_SCAN_MAX_INTERVAL_SECONDS or min_interval)
    deadline = time.time() + max(10, timeout)
    min_interval = max(2, min_interval)
    max_interval = max(min_interval, max_interval)
    last_result = {}
    while True:
        last_result = await asyncio.to_thread(scan_warm_account_probe, mailbox_email, token, subject)
        placement = last_result.get("placement")
        status = last_result.get("status")
        if placement in {"inbox", "spam", "other"} or status in {"found", "gmail_api_required", "missing_gmail_readonly_scope", "gmail_api_unavailable", "error", "missing_sender"}:
            return last_result
        if time.time() >= deadline:
            return last_result
        await asyncio.sleep(min(random.uniform(min_interval, max_interval), max(1, deadline - time.time())))


async def full_auto_inbox_rescue(mailbox_email, lookup, subject="", initial_result=None, max_attempts=3):
    result = initial_result or await scan_warm_account_probe_automatically(mailbox_email, lookup, subject=subject)
    capability = warm_inbox_rescue_capability(mailbox_email)
    moves = []
    if result.get("placement") == "inbox":
        return {"placement": "inbox", "result": "inbox_ready", "capability": capability, "scan": result, "moves": moves}
    if result.get("placement") != "spam":
        return {
            "placement": result.get("placement", "missing"),
            "result": "not_inbox",
            "capability": capability,
            "scan": result,
            "moves": moves,
        }
    if not capability.get("capable"):
        return {
            "placement": result.get("placement", "spam"),
            "result": "auto_rescue_unavailable",
            "capability": capability,
            "scan": result,
            "moves": [],
        }

    for attempt in range(1, max(1, int(max_attempts or 3)) + 1):
        move_result = await asyncio.to_thread(move_warm_account_probe_to_inbox, mailbox_email, result)
        move_result["attempt"] = attempt
        moves.append(move_result)
        if not move_result.get("moved"):
            continue
        await asyncio.sleep(min(8, 2 + attempt * 2))
        result = await scan_warm_account_probe_automatically(
            mailbox_email,
            lookup,
            subject=subject,
            timeout_seconds=WARM_PROBE_RESCAN_TIMEOUT_SECONDS,
        )
        if result.get("placement") == "inbox":
            return {
                "placement": "inbox",
                "result": "rescued_to_inbox",
                "capability": capability,
                "scan": result,
                "moves": moves,
            }
        if result.get("placement") != "spam":
            break

    return {
        "placement": result.get("placement", "spam"),
        "result": "auto_rescue_failed",
        "capability": capability,
        "scan": result,
        "moves": moves,
    }


def gmail_sender_emails():
    emails = []
    for sender in list_senders():
        email = normalize_email(sender.get("email", ""))
        if sender_is_gmail_api_or_gmail(sender):
            emails.append(email)
    return list(dict.fromkeys(emails))


def sender_is_gmail_api_or_gmail(sender_or_email):
    if isinstance(sender_or_email, dict):
        email = normalize_email(sender_or_email.get("email", ""))
        auth_method = (sender_or_email.get("auth_method") or "").strip().lower()
    else:
        email = normalize_email(str(sender_or_email or ""))
        sender = get_sender(email)
        auth_method = (sender or {}).get("auth_method", "").strip().lower() if sender else ""
    return bool(email and (auth_method == "gmail_api" or detect_provider(email) == "gmail"))


def _unique_form_emails(form, multi_name, single_name=""):
    values = []
    for value in form.getlist(multi_name):
        email = normalize_email(value)
        if email:
            values.append(email)
    if single_name:
        email = normalize_email(form.get(single_name, ""))
        if email:
            values.append(email)
    return list(dict.fromkeys(values))


def _parse_warm_invite(invite_text):
    text = str(invite_text or "").strip()
    if not text:
        return "", ""
    cluster_match = re.search(r"(?:cluster\s*id|cluster_id)\s*[:：]\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
    secret_match = re.search(r"(?:cluster\s*secret|cluster_secret)\s*[:：]\s*([A-Za-z0-9_.=-]+)", text, re.IGNORECASE)
    cluster_id = cluster_match.group(1).strip() if cluster_match else ""
    cluster_secret = secret_match.group(1).strip() if secret_match else ""
    if cluster_id and cluster_secret:
        return cluster_id, cluster_secret

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not cluster_id:
        cluster_id = next((line for line in lines if line.startswith("wcl_")), "")
    if not cluster_secret and len(lines) >= 2:
        cluster_secret = lines[1] if lines[0] == cluster_id else ""
    return cluster_id, cluster_secret


def _warm_mailbox_status_map(mailboxes):
    return {
        normalize_email(row.get("email", "")): row
        for row in mailboxes
        if normalize_email(row.get("email", ""))
    }


def _warm_sender_capability_status(sender, mailbox_by_email):
    email = normalize_email(sender.get("email", ""))
    mailbox = mailbox_by_email.get(email) or {}
    mailbox_status = (mailbox.get("status") or "").strip().lower()
    if mailbox_status == "active":
        return {
            **sender,
            "warm_status": mailbox_status,
            "warm_status_label": "Already active",
            "warm_status_message": "This sender is already in the selected warm flow.",
            "warm_selectable": False,
            "warm_mailbox": mailbox,
        }
    if mailbox_status == "pending":
        return {
            **sender,
            "warm_status": mailbox_status,
            "warm_status_label": "Pending approval",
            "warm_status_message": "Pending locally. Select again to resubmit and confirm the remote join request.",
            "warm_selectable": True,
            "warm_mailbox": mailbox,
        }

    if not sender_is_gmail_api_or_gmail(sender):
        return {
            **sender,
            "warm_status": "not_gmail_api_sender",
            "warm_status_label": "Not Gmail API sender",
            "warm_status_message": "Use a Gmail API / Workspace sender for Full Auto Warm.",
            "warm_selectable": False,
            "warm_mailbox": mailbox,
        }

    capability = warm_inbox_rescue_capability(email)
    capability_status = capability.get("status", "")
    if capability.get("capable"):
        return {
            **sender,
            "warm_status": "ready",
            "warm_status_label": "Ready",
            "warm_status_message": "Can scan, rescue to Inbox, and send the ownership reply after submission.",
            "warm_selectable": True,
            "warm_mailbox": mailbox,
            "warm_capability": capability,
        }
    status = "gmail_api_unavailable"
    label = "Unavailable"
    return {
        **sender,
        "warm_status": status,
        "warm_status_label": label,
        "warm_status_message": capability.get("message") or GMAIL_MODIFY_SETUP_HINT,
        "warm_selectable": False,
        "warm_mailbox": mailbox,
        "warm_capability": capability,
    }


def build_warm_sender_options(senders, mailboxes):
    mailbox_by_email = _warm_mailbox_status_map(mailboxes)
    return [_warm_sender_capability_status(sender, mailbox_by_email) for sender in senders]


def _record_warm_probe_session(request, probe):
    request.session["warm_account_probe"] = probe
    scan = probe.get("scan_after_move") if probe.get("scan_after_move") else probe.get("scan")
    if scan:
        store_warm_account_probe_scan(request, probe, scan)


def warm_mailbox_probe_failure_message(probe):
    result = probe.get("result", probe.get("status", "unknown"))
    if probe.get("result") == "auto_rescue_unavailable":
        placement = ((probe.get("rescue") or {}).get("placement") or (probe.get("scan") or {}).get("placement") or "").lower()
        if placement == "spam":
            return "The probe landed in Spam and automatic rescue is not enabled. Move it to Inbox manually, then run verification again; or reconnect Gmail with automatic Spam-to-Inbox rescue enabled."
        return (probe.get("capability") or {}).get("message") or GMAIL_MODIFY_SETUP_HINT
    if probe.get("result") == "auto_rescue_failed":
        return "Auto Inbox rescue failed after retries. Check Gmail access, then retry."
    if probe.get("result") == "not_sent" and (
        probe.get("error") == "mailbox_bound_to_another_user"
        or probe.get("message") == "mailbox_bound_to_another_user"
        or probe.get("reason") == "mailbox_bound_to_another_user"
    ):
        return (
            "Verification result: not_sent. This mailbox is already bound to another ePetrel Warm login. "
            "Log in with the ePetrel account that first verified this mailbox, or ask ePetrel support/admin to reset the mailbox ownership binding."
        )
    probe_reason = (
        probe.get("error")
        or probe.get("message")
        or probe.get("reason")
        or probe.get("detail")
        or probe.get("status_message")
        or ""
    )
    message = f"Verification result: {result}."
    if probe_reason:
        message += f" {probe_reason}"
    return message


async def verify_warm_mailbox_for_operation(request, auth_data, email):
    probe = await run_warm_mailbox_ownership_probe(auth_data, email)
    _record_warm_probe_session(request, probe)
    if probe.get("result") in {"verified", "already_verified"}:
        return {"ok": True, "probe": probe}
    message = warm_mailbox_probe_failure_message(probe)
    return {"ok": False, "probe": probe, "error": message}


def _upsert_local_warm_mailbox(cluster_id, email, status, daily_limit, timezone, policy):
    upsert_warm_mailbox(
        email,
        cluster_id=cluster_id,
        provider=detect_provider(email),
        status=status,
        daily_limit=daily_limit,
        timezone=timezone,
        capabilities="send,scan,reply,inbox_rescue",
        scan_soft_timeout_hours=policy["scan_soft_timeout_hours"],
        scan_hard_timeout_hours=policy["scan_hard_timeout_hours"],
        reply_min_delay_hours=policy["reply_min_delay_hours"],
        reply_hard_timeout_hours=policy["reply_hard_timeout_hours"],
        avoid_sleep_hours=bool(policy.get("avoid_sleep_hours", True)),
        avoid_weekends=bool(policy["avoid_weekends"]),
    )
    upsert_warm_cluster_member(
        cluster_id,
        email,
        provider=detect_provider(email),
        status=status,
        capabilities="send,scan,reply,inbox_rescue",
        daily_limit=daily_limit,
        timezone=timezone,
    )


def _warm_remote_policy_payload(policy, daily_limit=None, timezone=None):
    return {
        "daily_limit": daily_limit,
        "timezone": timezone or policy.get("timezone", ""),
        "scan_soft_timeout_hours": policy.get("scan_soft_timeout_hours"),
        "scan_hard_timeout_hours": policy.get("scan_hard_timeout_hours"),
        "reply_min_delay_hours": policy.get("reply_min_delay_hours"),
        "reply_hard_timeout_hours": policy.get("reply_hard_timeout_hours"),
        "sleep_start_hour": policy.get("sleep_start_hour"),
        "sleep_end_hour": policy.get("sleep_end_hour"),
        "avoid_sleep_hours": bool(policy.get("avoid_sleep_hours", True)),
        "avoid_weekends": bool(policy.get("avoid_weekends", True)),
    }


def summarize_warm_batch_results(results, active_label="active", pending_label="pending approval"):
    active = [item["email"] for item in results if item.get("status") == "active"]
    pending = [item["email"] for item in results if item.get("status") == "pending"]
    failed = [item for item in results if item.get("status") == "failed"]
    parts = []
    if active:
        parts.append(f"{len(active)} {active_label}")
    if pending:
        parts.append(f"{len(pending)} {pending_label}")
    if failed:
        parts.append(f"{len(failed)} failed")
    summary = "; ".join(parts) if parts else "No mailboxes changed"
    if failed:
        details = "; ".join(f"{item.get('email')}: {item.get('error')}" for item in failed[:5])
        summary = f"{summary}. {details}"
    return summary


async def process_warm_mailbox_registration(request, auth_data, cluster_id, email, daily_limit, timezone, remote_state=None):
    policy = warm_policy_config()
    sender = get_sender(email)
    if not sender:
        return {"email": email, "status": "failed", "error": "Sender is not saved locally."}
    if not sender_is_gmail_api_or_gmail(sender):
        return {"email": email, "status": "failed", "error": "Use a Gmail API / Workspace sender for Full Auto Warm."}

    cluster = get_warm_cluster(cluster_id, include_secrets=True)
    if remote_state is None:
        remote_state = sync_remote_warm_cluster_state(auth_data, cluster_id)
    remote_members = {
        normalize_email(row.get("email", "")): row
        for row in remote_state.get("members", [])
    }
    member_status = (remote_members.get(email) or {}).get("status", "")
    is_remote_owner = bool(remote_state.get("is_owner"))
    effective_status = "active" if is_remote_owner or member_status == "active" else "pending"

    try:
        verified = await verify_warm_mailbox_for_operation(request, auth_data, email)
    except WarmApiError as exc:
        return {"email": email, "status": "failed", "error": str(exc)}
    except Exception as exc:
        return {"email": email, "status": "failed", "error": str(exc)}
    if not verified.get("ok"):
        return {"email": email, "status": "failed", "error": verified.get("error", "Mailbox verification failed.")}

    if effective_status == "pending":
        try:
            join_warm_cluster(
                auth_data["access_token"],
                {
                    "cluster_id": cluster_id,
                    "email": email,
                    "provider": detect_provider(email),
                    "capabilities": ["send", "scan", "reply", "inbox_rescue"],
                    **_warm_remote_policy_payload(policy, daily_limit=daily_limit, timezone=timezone),
                },
            )
        except WarmApiError as exc:
            return {"email": email, "status": "failed", "error": f"Join request failed: {exc}"}
        _upsert_local_warm_mailbox(cluster_id, email, "pending", daily_limit, timezone, policy)
        log_warm_event(cluster_id=cluster_id, mailbox_email=email, event_type="join_requested", status="pending")
        return {"email": email, "status": "pending"}

    register_payload = {
        "cluster_id": cluster_id,
        "owner_email": cluster.get("owner_email", ""),
        "email": email,
        "provider": detect_provider(email),
        "status": "active",
        "capabilities": ["send", "scan", "reply", "inbox_rescue"],
        **_warm_remote_policy_payload(policy, daily_limit=daily_limit, timezone=timezone),
    }
    if cluster.get("owner_private_key"):
        register_payload.update(make_owner_signature(cluster.get("owner_private_key", ""), cluster_id, "approve", email))
    try:
        register_warm_mailbox(auth_data["access_token"], register_payload)
    except WarmApiError as exc:
        if "Cluster Owner must approve this mailbox" in str(exc) and is_remote_owner:
            try:
                await ensure_warm_member_active_for_registration(auth_data, cluster, cluster_id, email, daily_limit, timezone)
                register_warm_mailbox(auth_data["access_token"], register_payload)
            except WarmApiError as retry_exc:
                return {"email": email, "status": "failed", "error": f"Registration failed after owner auto-approval: {retry_exc}"}
        elif "Cluster Owner must approve this mailbox" in str(exc):
            _upsert_local_warm_mailbox(cluster_id, email, "pending", daily_limit, timezone, policy)
            return {"email": email, "status": "pending"}
        else:
            return {"email": email, "status": "failed", "error": f"Mailbox registration failed: {exc}"}

    _upsert_local_warm_mailbox(cluster_id, email, "active", daily_limit, timezone, policy)
    log_warm_event(cluster_id=cluster_id, mailbox_email=email, event_type="mailbox_registered", status="active", details="local client registration")
    return {"email": email, "status": "active"}


async def process_warm_join_request(request, auth_data, cluster_id, cluster_secret, email, daily_limit, timezone):
    policy = warm_policy_config()
    sender = get_sender(email)
    if not sender:
        return {"email": email, "status": "failed", "error": "Sender is not saved locally."}
    if not sender_is_gmail_api_or_gmail(sender):
        return {"email": email, "status": "failed", "error": "Use a Gmail API / Workspace sender for Full Auto Warm."}
    try:
        verified = await verify_warm_mailbox_for_operation(request, auth_data, email)
    except WarmApiError as exc:
        return {"email": email, "status": "failed", "error": str(exc)}
    except Exception as exc:
        return {"email": email, "status": "failed", "error": str(exc)}
    if not verified.get("ok"):
        return {"email": email, "status": "failed", "error": verified.get("error", "Mailbox verification failed.")}
    try:
        join_response = join_warm_cluster(
            auth_data["access_token"],
            {
                "cluster_id": cluster_id,
                "email": email,
                "provider": detect_provider(email),
                "capabilities": ["send", "scan", "reply", "inbox_rescue"],
                **_warm_remote_policy_payload(policy, daily_limit=daily_limit, timezone=timezone),
            },
        )
    except WarmApiError as exc:
        return {"email": email, "status": "failed", "error": f"Join request failed: {exc}"}
    try:
        remote_state = fetch_warm_cluster_members(auth_data["access_token"], cluster_id)
    except WarmApiError as exc:
        return {"email": email, "status": "failed", "error": f"Join request was accepted but remote confirmation failed: {exc}"}
    remote_members = {
        normalize_email(row.get("email", "")): row
        for row in (remote_state.get("members") or [])
        if isinstance(row, dict)
    }
    remote_status = (remote_members.get(email) or {}).get("status", "")
    if remote_status not in {"pending", "active"}:
        join_status = join_response.get("status", "") if isinstance(join_response, dict) else ""
        return {
            "email": email,
            "status": "failed",
            "error": f"Join request returned {join_status or 'success'}, but this mailbox was not found in remote members.",
        }
    upsert_warm_cluster(
        cluster_id,
        name=f"Joined Cluster {cluster_id[-6:]}",
        owner_public_key=derive_owner_public_key(cluster_secret),
        role="member",
        status=remote_status,
        cluster_secret=cluster_secret,
    )
    _upsert_local_warm_mailbox(cluster_id, email, remote_status, daily_limit, timezone, policy)
    log_warm_event(cluster_id=cluster_id, mailbox_email=email, event_type="join_requested", status=remote_status)
    return {"email": email, "status": remote_status}


def normalize_message_id_for_warm(message_id):
    return str(message_id or "").strip().strip("<>").strip()


async def run_warm_mailbox_ownership_probe(auth_data, mailbox_email):
    start = start_warm_mailbox_ownership(auth_data["access_token"], {"mailboxes": [mailbox_email]})
    probes = start.get("probes") or []
    probe = next((item for item in probes if normalize_email(item.get("mailbox_email", "")) == mailbox_email), probes[0] if probes else {})
    if probe.get("status") == "verified":
        return {**probe, "mailbox_email": mailbox_email, "result": "already_verified"}
    if probe.get("status") != "sent":
        return {**probe, "mailbox_email": mailbox_email, "result": "not_sent"}

    lookup = probe.get("probe_id", "")
    result = await scan_warm_account_probe_automatically(mailbox_email, lookup, subject=probe.get("subject", ""))
    probe = {**probe, "mailbox_email": mailbox_email, "to_email": mailbox_email, "scan": result}
    rescue = await full_auto_inbox_rescue(mailbox_email, lookup, subject=probe.get("subject", ""), initial_result=result)
    probe["rescue"] = rescue
    if rescue.get("moves"):
        probe["move"] = rescue["moves"][-1]
    result = rescue.get("scan") or result
    if result is not probe.get("scan"):
        probe["scan_after_move"] = result
    placement = rescue.get("placement") or result.get("placement")

    if placement != "inbox":
        return {**probe, "result": rescue.get("result") or "not_inbox"}

    verification_token = result.get("verification_token", "")
    if not verification_token:
        return {**probe, "result": "missing_token"}

    reply_min_delay = max(0, int(WARM_PROBE_REPLY_MIN_DELAY_SECONDS or 0))
    reply_max_delay = max(reply_min_delay, int(WARM_PROBE_REPLY_MAX_DELAY_SECONDS or reply_min_delay))
    if reply_max_delay:
        await asyncio.sleep(random.uniform(reply_min_delay, reply_max_delay))
    reply = await asyncio.to_thread(send_warm_account_probe_reply, mailbox_email, result)
    probe["reply"] = reply
    if not reply.get("sent"):
        return {**probe, "result": "reply_failed"}

    reply_message_id = normalize_message_id_for_warm(reply.get("message_id") or result.get("message_id") or result.get("rfc822_message_id"))
    if not reply_message_id:
        return {**probe, "result": "missing_reply_message_id"}
    probe["reply_message_id"] = reply_message_id

    report_warm_mailbox_ownership_reply(
        auth_data["access_token"],
        {
            "mailbox_email": mailbox_email,
            "message_id": reply_message_id,
            "probe_id": probe.get("probe_id", ""),
        },
    )
    verify = verify_warm_mailbox_ownership(
        auth_data["access_token"],
        {
            "mailbox_email": mailbox_email,
            "verification_token": verification_token,
            "placement": placement,
            "reply_message_id": reply_message_id,
            "probe_id": probe.get("probe_id", ""),
        },
    )
    return {**probe, "verify": verify, "result": "verified"}


async def ensure_warm_mailbox_verified(request, auth_data, email, action_label="continuing"):
    try:
        probe = await run_warm_mailbox_ownership_probe(auth_data, email)
    except WarmApiError as exc:
        flash(request, "error", f"Warm mailbox verification failed before {action_label}: {exc}")
        return False

    request.session["warm_account_probe"] = probe
    scan = probe.get("scan_after_move") if probe.get("scan_after_move") else probe.get("scan")
    if scan:
        store_warm_account_probe_scan(request, probe, scan)

    if probe.get("result") in {"verified", "already_verified"}:
        return True

    flash(request, "warning", f"Verify this warm mailbox before {action_label}: {email}. {warm_mailbox_probe_failure_message(probe)}")
    return False


async def ensure_warm_member_active_for_registration(auth_data, cluster, cluster_id, email, daily_limit, timezone):
    if not cluster or cluster.get("role") != "owner":
        return False
    policy = warm_policy_config()
    signature = make_owner_signature(cluster.get("owner_private_key", ""), cluster_id, "approve", email)
    try:
        join_warm_cluster(
            auth_data["access_token"],
            {
                "cluster_id": cluster_id,
                "email": email,
                "provider": detect_provider(email),
                "capabilities": ["send", "scan", "reply", "inbox_rescue"],
                **_warm_remote_policy_payload(policy, daily_limit=daily_limit, timezone=timezone),
            },
        )
    except WarmApiError as exc:
        if "blacklisted" in str(exc).lower():
            raise

    approve_warm_cluster_member(auth_data["access_token"], cluster_id, email, signature)
    update_warm_cluster_member_status(cluster_id, email, "active")
    return True


def warm_auth_wp_user_id(auth_data):
    user = (auth_data or {}).get("user") or {}
    try:
        return int(user.get("wp_user_id") or (auth_data or {}).get("wp_user_id") or 0)
    except (TypeError, ValueError):
        return 0


def sync_remote_warm_cluster_state(auth_data, cluster_id):
    local_cluster = get_warm_cluster(cluster_id, include_secrets=True)
    owner_payload = {}
    if local_cluster.get("owner_private_key"):
        owner_payload = make_owner_signature(local_cluster.get("owner_private_key", ""), cluster_id, "members_read")
    response = fetch_warm_cluster_members(auth_data["access_token"], cluster_id, owner_payload=owner_payload)
    remote_cluster = response.get("cluster") if isinstance(response.get("cluster"), dict) else {}
    remote_members = response.get("members") or []
    current_wp_user_id = warm_auth_wp_user_id(auth_data)
    remote_owner_user_id = int(remote_cluster.get("owner_user_id") or 0)
    has_local_owner_key = bool(local_cluster.get("owner_private_key"))
    is_remote_owner_login = bool(current_wp_user_id and remote_owner_user_id == current_wp_user_id)
    remote_role = "owner" if has_local_owner_key or is_remote_owner_login else "member"
    upsert_warm_cluster(
        cluster_id,
        name=remote_cluster.get("name") or local_cluster.get("name", ""),
        owner_email=remote_cluster.get("owner_email") or local_cluster.get("owner_email", ""),
        owner_public_key=local_cluster.get("owner_public_key", ""),
        role=remote_role,
        status=remote_cluster.get("status") or local_cluster.get("status", "active"),
        cluster_secret=local_cluster.get("cluster_secret", ""),
        owner_private_key=local_cluster.get("owner_private_key", ""),
    )
    if (remote_cluster.get("status") or "").strip().lower() == "dissolved":
        mark_warm_cluster_dissolved(cluster_id)
    remote_emails = set()
    for row in remote_members:
        email = normalize_email(row.get("email", ""))
        if not email:
            continue
        remote_emails.add(email)
        row_status = row.get("status", "pending")
        upsert_warm_cluster_member(
            cluster_id,
            email,
            provider=row.get("provider", ""),
            status=row_status,
            capabilities=",".join(row.get("capabilities", [])) if isinstance(row.get("capabilities"), list) else row.get("capabilities", ""),
            daily_limit=int(row.get("daily_limit") or 5),
            timezone=row.get("timezone", ""),
        )
        if row_status in {"active", "paused", "pending", "blacklisted"}:
            update_warm_mailbox_status(email, "paused" if row_status == "blacklisted" else row_status)
    if current_wp_user_id and remote_owner_user_id == current_wp_user_id and remote_members:
        for local_member in list_warm_cluster_members(cluster_id):
            local_email = normalize_email(local_member.get("email", ""))
            if local_email and local_email not in remote_emails:
                delete_warm_cluster_member(cluster_id, local_email)
                delete_warm_mailbox(local_email, cluster_id)
    return {
        "cluster": remote_cluster,
        "members": remote_members,
        "member_count": len(remote_members),
        "local_member_count": len(list_warm_cluster_members(cluster_id)),
        "current_wp_user_id": current_wp_user_id,
        "remote_owner_user_id": remote_owner_user_id,
        "has_local_owner_key": has_local_owner_key,
        "is_remote_owner_login": is_remote_owner_login,
        "role": remote_role,
        "is_owner": remote_role == "owner",
    }


def _warm_first_value(row, keys, default=""):
    if not isinstance(row, dict):
        return default
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _warm_int(value, default=0):
    try:
        if isinstance(value, str):
            value = value.strip().replace("%", "")
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _warm_rate(value, numerator=0, denominator=0):
    try:
        if value is not None and value != "":
            if isinstance(value, str):
                value = value.strip().replace("%", "")
            parsed = float(value)
            return parsed / 100 if parsed > 1 else parsed
    except (TypeError, ValueError):
        pass
    return numerator / denominator if denominator else 0


def _warm_remote_mailbox_candidates(summary):
    if not isinstance(summary, dict):
        return []
    for key in ("mailbox_rows", "mailboxes", "rows", "member_rows", "members", "by_mailbox"):
        value = summary.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            rows = []
            for email, row in value.items():
                if isinstance(row, dict):
                    rows.append({"email": email, **row})
            if rows:
                return rows
    return []


def _warm_parse_time(value):
    value = str(value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _warm_mailbox_health_from_seen(last_seen_at, existing_status="", existing_reason="", existing_seconds=None):
    if existing_status:
        return {
            "last_seen_at": str(last_seen_at or ""),
            "seconds_since_seen": existing_seconds,
            "health_status": str(existing_status),
            "health_reason": str(existing_reason or ""),
        }
    seen = _warm_parse_time(last_seen_at)
    if not seen:
        return {
            "last_seen_at": str(last_seen_at or ""),
            "seconds_since_seen": None,
            "health_status": "never_seen",
            "health_reason": "No successful heartbeat has been recorded for this mailbox.",
        }
    age = max(0, int((datetime.utcnow() - seen).total_seconds()))
    warn_sec = max(60, int(WARM_MAILBOX_OFFLINE_WARN_SEC or 3600))
    stale_sec = max(warn_sec, int(WARM_MAILBOX_STALE_SEC or 259200))
    if age >= stale_sec:
        return {
            "last_seen_at": str(last_seen_at or ""),
            "seconds_since_seen": age,
            "health_status": "stale_lost_task_risk",
            "health_reason": "No heartbeat for 72h+; queued Redis tasks may have expired.",
        }
    if age >= warn_sec:
        return {
            "last_seen_at": str(last_seen_at or ""),
            "seconds_since_seen": age,
            "health_status": "offline_warning",
            "health_reason": "No heartbeat for 1h+.",
        }
    return {
        "last_seen_at": str(last_seen_at or ""),
        "seconds_since_seen": age,
        "health_status": "online",
        "health_reason": "Recent heartbeat received.",
    }


def _normalize_warm_remote_mailbox_row(row):
    email = normalize_email(_warm_first_value(row, ("email", "mailbox_email", "sender_email", "address")))
    if not email:
        return {}
    sent_count = _warm_int(_warm_first_value(row, ("sent_count", "send_count", "sent", "initial_sent")))
    reply_count = _warm_int(_warm_first_value(row, ("reply_count", "replies", "replied_count", "reply")))
    placement_count = _warm_int(_warm_first_value(row, ("received_count", "placement_count", "scanned_count", "scan_count", "received", "scanned")))
    inbox_count = _warm_int(_warm_first_value(row, ("inbox_count", "inbox")))
    spam_count = _warm_int(_warm_first_value(row, ("spam_count", "spam", "junk_count", "junk")))
    other_count = _warm_int(_warm_first_value(row, ("other_count", "other")))
    missing_count = _warm_int(_warm_first_value(row, ("missing_count", "missing")))
    last_seen_at = str(_warm_first_value(row, ("last_seen_at", "last_heartbeat_at"), ""))
    health = _warm_mailbox_health_from_seen(
        last_seen_at,
        _warm_first_value(row, ("health_status",), ""),
        _warm_first_value(row, ("health_reason",), ""),
        _warm_first_value(row, ("seconds_since_seen",), None),
    )
    return {
        "email": email,
        "sent_count": sent_count,
        "reply_count": reply_count,
        "received_count": placement_count,
        "placement_count": placement_count,
        "inbox_count": inbox_count,
        "spam_count": spam_count,
        "other_count": other_count,
        "missing_count": missing_count,
        "inbox_rate": _warm_rate(_warm_first_value(row, ("inbox_rate", "inbox_percent", "inbox_percentage"), None), inbox_count, placement_count),
        "spam_rate": _warm_rate(_warm_first_value(row, ("spam_rate", "spam_percent", "spam_percentage"), None), spam_count, placement_count),
        "last_event_at": str(_warm_first_value(row, ("last_event_at", "last_activity", "last_activity_at", "updated_at", "last_seen_at"), "")),
        "worker_status": str(_warm_first_value(row, ("worker_status", "worker", "status"), "")),
        "claim_message": str(_warm_first_value(row, ("claim_message", "message", "scheduler_message"), "")),
        "scheduler": str(_warm_first_value(row, ("scheduler",), "")),
        "last_claim_at": str(_warm_first_value(row, ("last_claim_at",), "")),
        "last_heartbeat_at": str(_warm_first_value(row, ("last_heartbeat_at", "last_seen_at"), "")),
        "last_error": str(_warm_first_value(row, ("last_error", "error"), "")),
        "removable_by_owner": bool(_warm_first_value(row, ("removable_by_owner",), False)),
        "row_source": "Remote",
        **health,
    }


def merge_warm_summary_for_display(local_summary, remote_summary):
    summary = dict(local_summary or {})
    local_rows = [dict(row, row_source=row.get("row_source") or "Local") for row in summary.get("mailbox_rows", [])]
    remote_rows = [
        normalized
        for normalized in (_normalize_warm_remote_mailbox_row(row) for row in _warm_remote_mailbox_candidates(remote_summary))
        if normalized
    ]
    if isinstance(remote_summary, dict) and remote_summary:
        sent_count = _warm_int(_warm_first_value(remote_summary, ("sent_count", "sent"), summary.get("sent_count", 0)))
        reply_count = _warm_int(_warm_first_value(remote_summary, ("reply_count", "replies"), summary.get("reply_count", 0)))
        placement_count = _warm_int(_warm_first_value(remote_summary, ("received_count", "placement_count", "scanned_count"), summary.get("placement_count", 0)))
        inbox_count = _warm_int(_warm_first_value(remote_summary, ("inbox_count", "inbox"), summary.get("inbox_count", 0)))
        spam_count = _warm_int(_warm_first_value(remote_summary, ("spam_count", "spam"), summary.get("spam_count", 0)))
        other_count = _warm_int(_warm_first_value(remote_summary, ("other_count", "other"), summary.get("other_count", 0)))
        missing_count = _warm_int(_warm_first_value(remote_summary, ("missing_count", "missing"), summary.get("missing_count", 0)))
        summary.update({
            "scope": "cluster",
            "sent_count": sent_count,
            "reply_count": reply_count,
            "sent_total": _warm_int(_warm_first_value(remote_summary, ("sent_total",), sent_count + reply_count)),
            "received_count": placement_count,
            "placement_count": placement_count,
            "inbox_count": inbox_count,
            "spam_count": spam_count,
            "other_count": other_count,
            "missing_count": missing_count,
            "inbox_rate": _warm_rate(_warm_first_value(remote_summary, ("inbox_rate", "inbox_percent", "inbox_percentage"), None), inbox_count, placement_count),
            "spam_rate": _warm_rate(_warm_first_value(remote_summary, ("spam_rate", "spam_percent", "spam_percentage"), None), spam_count, placement_count),
            "remote_mailbox_rows_count": len(remote_rows),
            "remote_has_mailbox_rows": bool(remote_rows),
        })
        remote_active = _warm_int(_warm_first_value(remote_summary, ("active_mailboxes", "active_mailbox_count", "mailbox_count", "member_count"), 0))
        summary["active_mailboxes"] = max(_warm_int(summary.get("active_mailboxes")), remote_active, len(remote_rows))
    else:
        summary["remote_mailbox_rows_count"] = 0
        summary["remote_has_mailbox_rows"] = False

    merged_by_email = {}
    for row in local_rows:
        email = normalize_email(row.get("email", ""))
        if email:
            merged_by_email[email] = row
    for row in remote_rows:
        email = row["email"]
        local_row = merged_by_email.get(email, {})
        merged = {**local_row, **row}
        for key in ("worker_status", "claim_message", "scheduler", "last_claim_at", "last_heartbeat_at", "last_error"):
            if not merged.get(key) and local_row.get(key):
                merged[key] = local_row.get(key)
        merged_by_email[email] = merged
    summary["mailbox_rows"] = list(merged_by_email.values())
    return summary


def prepare_warm_summary_for_display(summary, selected_cluster=None):
    result = dict(summary or {})
    is_owner = bool(selected_cluster and selected_cluster.get("role") == "owner" and selected_cluster.get("owner_private_key"))
    rows = []
    for row in result.get("mailbox_rows", []) or []:
        item = dict(row)
        last_seen_at = item.get("last_seen_at") or item.get("last_heartbeat_at") or ""
        if not item.get("health_status"):
            item.update(_warm_mailbox_health_from_seen(last_seen_at))
        item["last_seen_at"] = item.get("last_seen_at") or last_seen_at
        item["can_remove_stale"] = bool(is_owner and item.get("health_status") == "stale_lost_task_risk")
        rows.append(item)
    result["mailbox_rows"] = rows
    result["last_refreshed_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    result["can_remove_stale_members"] = is_owner
    return result


@app.post("/warm/account-probe/send")
async def warm_account_probe_send_route(request: Request, mailbox_email: str = Form("")):
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")

    auth_user = auth_data.get("user") or {}
    email = normalize_email(mailbox_email) or normalize_email(auth_user.get("email", ""))
    if not email:
        flash(request, "error", "Choose the Gmail API / Workspace address used for this warm authorization.")
        return redirect("/warm")
    sender = get_sender(email)
    if not sender:
        flash(request, "error", "Save this Gmail API / Workspace mailbox in the local sender pool before running the account placement probe.")
        return redirect("/warm")
    if not sender_is_gmail_api_or_gmail(sender):
        flash(request, "error", "The account placement probe requires a saved Gmail API / Google Workspace sender or a Gmail mailbox.")
        return redirect("/warm")

    try:
        probe = await run_warm_mailbox_ownership_probe(auth_data, email)
        request.session["warm_account_probe"] = probe
        if probe.get("scan"):
            store_warm_account_probe_scan(request, probe, probe["scan_after_move"] if probe.get("scan_after_move") else probe["scan"])
        if probe.get("result") in {"verified", "already_verified"}:
            flash(request, "success", f"Warm mailbox verified: {email}.")
        elif probe.get("result") == "auto_rescue_unavailable":
            placement = ((probe.get("rescue") or {}).get("placement") or (probe.get("scan") or {}).get("placement") or "").lower()
            if placement == "spam":
                message = "The probe landed in Spam and automatic rescue is not enabled. Move it to Inbox manually, then run verification again; or reconnect Gmail with automatic Spam-to-Inbox rescue enabled."
            else:
                message = (probe.get("capability") or {}).get("message") or GMAIL_MODIFY_SETUP_HINT
            flash(request, "warning", f"Full Auto Warm is not enabled for {email}. {message}")
        elif probe.get("result") == "auto_rescue_failed":
            flash(request, "warning", f"Auto Inbox rescue failed after retries for {email}. The client did not reply. Check Gmail access, then retry verification.")
        else:
            flash(request, "warning", f"Warm mailbox verification did not complete for {email}: {probe.get('result', probe.get('status', 'unknown'))}.")
    except WarmApiError as exc:
        flash(request, "error", f"Warm mailbox verification failed: {exc}")
    return redirect("/warm")


@app.post("/warm/ownership/verify-all")
async def warm_ownership_verify_all_route(request: Request):
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")
    emails = gmail_sender_emails()
    if not emails:
        flash(request, "error", "Save at least one Gmail API / Workspace sender before verifying warm ownership.")
        return redirect("/warm")

    results = []
    for email in emails:
        try:
            results.append(await run_warm_mailbox_ownership_probe(auth_data, email))
        except WarmApiError as exc:
            results.append({"mailbox_email": email, "result": "api_error", "error": str(exc)})
    request.session["warm_ownership_results"] = results
    verified_count = sum(1 for item in results if item.get("result") in {"verified", "already_verified"})
    unavailable_count = sum(1 for item in results if item.get("result") == "auto_rescue_unavailable")
    rescue_failed_count = sum(1 for item in results if item.get("result") == "auto_rescue_failed")
    if unavailable_count:
        flash(request, "warning", f"Verified {verified_count}/{len(results)} Gmail API / Workspace senders. {unavailable_count} sender(s) need manual Spam-to-Inbox rescue, or Gmail reconnection with automatic rescue enabled.")
    elif rescue_failed_count:
        flash(request, "warning", f"Verified {verified_count}/{len(results)} Gmail API / Workspace senders. {rescue_failed_count} sender(s) could not be auto-rescued after retries.")
    else:
        flash(request, "success", f"Verified {verified_count}/{len(results)} Gmail API / Workspace senders.")
    return redirect("/warm")


@app.post("/warm/account-probe/scan")
async def warm_account_probe_scan_route(request: Request):
    probe = request.session.get("warm_account_probe") or {}
    email = normalize_email(probe.get("to_email", ""))
    token = str(probe.get("token", "")).strip()
    if not email or not token:
        flash(request, "error", "Send the ePetrel account placement email before scanning.")
        return redirect("/warm")

    result = await asyncio.to_thread(scan_warm_account_probe, email, token, probe.get("subject", ""))
    store_warm_account_probe_scan(request, probe, result)

    placement = result.get("placement")
    if placement == "inbox":
        flash(request, "success", "Found the ePetrel account email in Gmail Inbox.")
    elif placement == "spam":
        flash(request, "warning", "Found the ePetrel account email in Spam. Move it to Inbox manually, then run verification again; or reconnect Gmail with automatic Spam-to-Inbox rescue enabled.")
    elif result.get("status") in {"gmail_api_required", "missing_gmail_readonly_scope", "gmail_api_unavailable"}:
        flash(request, "warning", f"Reconnect Gmail API with Full Auto Warm scopes enabled. {GMAIL_MODIFY_SETUP_HINT}")
    else:
        flash(request, "warning", f"Account email not found yet. Scanner status: {result.get('status', 'missing')}.")
    return redirect("/warm")


@app.post("/warm/mailboxes")
async def save_warm_mailbox(
    request: Request,
    cluster_id: str = Form(""),
    sender_email: str = Form(""),
    daily_limit: int = Form(5),
    timezone: str = Form(""),
    status: str = Form("active"),
):
    lang = get_lang(request)
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")
    if not await ensure_warm_llm_ready(request, action_label="enabling warm mailboxes"):
        return redirect("/config")
    form = await request.form()
    emails = _unique_form_emails(form, "sender_emails", "sender_email")
    cluster_id = cluster_id.strip() or request.session.get("warm_cluster_id") or ""
    if not emails:
        flash(request, "error", t(lang, "warm_sender_required"))
        return redirect("/warm")
    if not cluster_id:
        flash(request, "error", "Create or join a Private Trust Cluster before enabling a warm mailbox.")
        return redirect("/warm")
    daily_limit = max(1, min(int(daily_limit or 5), 25))
    policy = warm_policy_config()
    timezone = timezone.strip() or policy["timezone"]
    try:
        remote_state = sync_remote_warm_cluster_state(auth_data, cluster_id)
    except WarmApiError as exc:
        flash(request, "error", f"Unable to confirm cluster ownership before enabling mailbox: {exc}")
        return redirect("/warm")
    results = []
    for email in emails:
        results.append(await process_warm_mailbox_registration(request, auth_data, cluster_id, email, daily_limit, timezone, remote_state=remote_state))
    summary = summarize_warm_batch_results(results)
    if any(item.get("status") in {"active", "pending"} for item in results):
        flash(request, "success", f"Warm mailbox batch completed: {summary}.")
    else:
        flash(request, "error", f"Warm mailbox batch failed: {summary}.")
    return redirect(f"/warm?cluster_id={cluster_id}")


@app.post("/warm/clusters")
async def create_warm_cluster_route(
    request: Request,
    name: str = Form(""),
    owner_email: str = Form(""),
):
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")
    if not await ensure_warm_llm_ready(request, action_label="creating a warm cluster"):
        return redirect("/config")
    owner_email = normalize_email(owner_email)
    if not owner_email:
        flash(request, "error", "Choose an owner warm mailbox before creating a cluster.")
        return redirect("/warm")
    if not await ensure_warm_mailbox_verified(request, auth_data, owner_email, action_label="creating a cluster"):
        return redirect("/warm")
    cluster_id = generate_cluster_id()
    cluster_secret = generate_cluster_secret()
    owner_private_key, owner_public_key = generate_owner_keypair()
    display_name = (name or "").strip() or f"Warm Cluster {cluster_id[-6:]}"
    policy = warm_policy_config()
    daily_limit = 5
    timezone = policy["timezone"]
    try:
        create_warm_cluster(
            auth_data["access_token"],
            {
                "cluster_id": cluster_id,
                "name": display_name,
                "owner_email": owner_email,
                "owner_public_key": owner_public_key,
                "status": "active",
                "provider": detect_provider(owner_email),
                "capabilities": ["send", "scan", "reply", "inbox_rescue"],
                **_warm_remote_policy_payload(policy, daily_limit=daily_limit, timezone=timezone),
            },
        )
    except WarmApiError as exc:
        if _warm_auth_error_is_invalid_token(exc):
            _clear_warm_auth(request)
            flash(request, "error", "Cluster creation failed: invalid warm token. Warm authorization was cleared; click Log in to ePetrel and authorize Warm again.")
        else:
            flash(request, "error", f"Cluster creation failed: {exc}")
        return redirect("/warm")
    upsert_warm_cluster(
        cluster_id,
        name=display_name,
        owner_email=owner_email,
        owner_public_key=owner_public_key,
        role="owner",
        status="active",
        cluster_secret=cluster_secret,
        owner_private_key=owner_private_key,
    )
    keep_only_warm_cluster(cluster_id)
    if owner_email:
        upsert_warm_cluster_member(cluster_id, owner_email, provider=detect_provider(owner_email), status="active")
        upsert_warm_mailbox(
            owner_email,
            cluster_id=cluster_id,
            provider=detect_provider(owner_email),
            status="active",
            daily_limit=daily_limit,
            timezone=timezone,
            capabilities="send,scan,reply,inbox_rescue",
            scan_soft_timeout_hours=policy["scan_soft_timeout_hours"],
            scan_hard_timeout_hours=policy["scan_hard_timeout_hours"],
            reply_min_delay_hours=policy["reply_min_delay_hours"],
            reply_hard_timeout_hours=policy["reply_hard_timeout_hours"],
            avoid_sleep_hours=bool(policy.get("avoid_sleep_hours", True)),
            avoid_weekends=bool(policy["avoid_weekends"]),
        )
    request.session["warm_cluster_id"] = cluster_id
    log_warm_event(cluster_id=cluster_id, mailbox_email=owner_email, event_type="cluster_created", status="active")
    flash(request, "success", f"Private warm cluster created: {display_name}.")
    return redirect(f"/warm?cluster_id={cluster_id}")


@app.post("/warm/clusters/join")
async def join_warm_cluster_route(
    request: Request,
    cluster_id: str = Form(""),
    cluster_secret: str = Form(""),
    member_email: str = Form(""),
    invite_text: str = Form(""),
    daily_limit: int = Form(5),
    timezone: str = Form(""),
):
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")
    if not await ensure_warm_llm_ready(request, action_label="joining a warm cluster"):
        return redirect("/config")
    form = await request.form()
    invite_cluster_id, invite_cluster_secret = _parse_warm_invite(invite_text or form.get("invite_text", ""))
    cluster_id = (cluster_id or invite_cluster_id).strip()
    cluster_secret = (cluster_secret or invite_cluster_secret).strip()
    emails = _unique_form_emails(form, "member_emails", "member_email")
    if not cluster_id or not cluster_secret or not emails:
        flash(request, "error", "Cluster invite and at least one mailbox are required.")
        return redirect("/warm")
    policy = warm_policy_config()
    daily_limit = max(1, min(int(daily_limit or 5), 25))
    timezone = timezone.strip() or policy["timezone"]
    results = []
    for email in emails:
        results.append(await process_warm_join_request(request, auth_data, cluster_id, cluster_secret, email, daily_limit, timezone))
    if any(item.get("status") in {"active", "pending"} for item in results):
        keep_only_warm_cluster(cluster_id)
    request.session["warm_cluster_id"] = cluster_id
    summary = summarize_warm_batch_results(results, active_label="active", pending_label="pending join request")
    if any(item.get("status") in {"active", "pending"} for item in results):
        if any(item.get("status") == "pending" for item in results):
            flash(request, "success", f"Join request batch completed: {summary}. The Cluster Owner must approve pending mailboxes before tasks start.")
        else:
            flash(request, "success", f"Join request batch completed: {summary}.")
    else:
        flash(request, "error", f"Join request batch failed: {summary}.")
    return redirect(f"/warm?cluster_id={cluster_id}")


@app.post("/warm/clusters/sync")
async def sync_warm_cluster_members_route(request: Request, cluster_id: str = Form("")):
    cluster_id = cluster_id.strip() or request.session.get("warm_cluster_id") or ""
    auth_data = _session_epetrel_auth(request, "warm_auth")
    if not cluster_id or not auth_data.get("access_token"):
        flash(request, "error", "Warm authorization and cluster selection are required.")
        return redirect("/warm")
    try:
        response = sync_remote_warm_cluster_state(auth_data, cluster_id)
        for row in response.get("members", []):
            row_status = row.get("status", "pending")
            if row.get("email") and row_status in {"active", "paused", "pending", "blacklisted"}:
                update_warm_mailbox_status(row.get("email", ""), "paused" if row_status == "blacklisted" else row_status)
        remote_count = int(response.get("member_count") or 0)
        local_count = int(response.get("local_member_count") or 0)
        owner_key_label = "yes" if response.get("has_local_owner_key") else "no"
        remote_owner_label = "yes" if response.get("is_remote_owner_login") else "no"
        sync_message = (
            f"Cluster member list synced: remote returned {remote_count}, local has {local_count}; "
            f"local owner key: {owner_key_label}; remote owner login: {remote_owner_label}."
        )
        if remote_count == 0 and local_count > 0:
            sync_message += (
                " Remote returned no members for this login, so local members were kept but remote scheduling may not see them; "
                "log in with the cluster owner's ePetrel account or re-register the warm mailboxes."
            )
        flash(
            request,
            "success" if remote_count else "warning",
            sync_message,
        )
    except WarmApiError as exc:
        if _warm_error_is_cluster_dissolved(exc):
            mark_warm_cluster_dissolved(cluster_id)
            flash(request, "warning", "This warm cluster has been dissolved by the owner. Local warm mailboxes were paused. 该 Warm 群已被群主解散，本地 Warm 邮箱已暂停。")
        else:
            flash(request, "error", f"Cluster member sync failed: {exc}")
    return redirect(f"/warm?cluster_id={cluster_id}")


@app.post("/warm/clusters/members/approve")
async def approve_warm_member_route(
    request: Request,
    cluster_id: str = Form(""),
    member_email: str = Form(""),
):
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")
    cluster_id = cluster_id.strip() or request.session.get("warm_cluster_id") or ""
    form = await request.form()
    member_emails = _unique_form_emails(form, "member_emails", "member_email")
    cluster = get_warm_cluster(cluster_id, include_secrets=True)
    if not cluster or cluster.get("role") != "owner":
        flash(request, "error", "Only the Cluster Owner can approve members.")
        return redirect("/warm")
    if not member_emails:
        flash(request, "error", "Choose at least one pending member to approve.")
        return redirect(f"/warm?cluster_id={cluster_id}")
    results = []
    for email in member_emails:
        signature = make_owner_signature(cluster.get("owner_private_key", ""), cluster_id, "approve", email)
        try:
            approve_warm_cluster_member(auth_data["access_token"], cluster_id, email, signature)
        except WarmApiError as exc:
            results.append({"email": email, "status": "failed", "error": str(exc)})
            continue
        update_warm_cluster_member_status(cluster_id, email, "active")
        sender = get_sender(email)
        if sender and sender_is_gmail_api_or_gmail(sender):
            policy = warm_policy_config()
            member = next(
                (row for row in list_warm_cluster_members(cluster_id) if normalize_email(row.get("email", "")) == email),
                {},
            )
            _upsert_local_warm_mailbox(
                cluster_id,
                email,
                "active",
                int(member.get("daily_limit") or 5),
                member.get("timezone") or policy["timezone"],
                policy,
            )
        else:
            update_warm_mailbox_status(email, "active")
        log_warm_event(cluster_id=cluster_id, mailbox_email=email, event_type="member_approved", status="active")
        results.append({"email": email, "status": "active"})
    summary = summarize_warm_batch_results(results, active_label="approved", pending_label="pending")
    if any(item.get("status") == "active" for item in results):
        flash(request, "success", f"Member approval completed: {summary}.")
    else:
        flash(request, "error", f"Member approval failed: {summary}.")
    return redirect(f"/warm?cluster_id={cluster_id}")


@app.post("/warm/clusters/members/remove")
async def remove_warm_member_route(
    request: Request,
    cluster_id: str = Form(""),
    member_email: str = Form(""),
):
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")
    cluster_id = cluster_id.strip() or request.session.get("warm_cluster_id") or ""
    member_email = normalize_email(member_email)
    cluster = get_warm_cluster(cluster_id, include_secrets=True)
    if not cluster or cluster.get("role") != "owner":
        flash(request, "error", "Only the Cluster Owner can remove members.")
        return redirect("/warm")
    signature = make_owner_signature(cluster.get("owner_private_key", ""), cluster_id, "remove", member_email)
    try:
        remove_warm_cluster_member(auth_data["access_token"], cluster_id, member_email, signature)
    except WarmApiError as exc:
        flash(request, "error", f"Member removal failed: {exc}")
        return redirect(f"/warm?cluster_id={cluster_id}")
    delete_warm_cluster_member(cluster_id, member_email)
    delete_warm_mailbox(member_email, cluster_id)
    log_warm_event(cluster_id=cluster_id, mailbox_email=member_email, event_type="member_removed", status="removed")
    flash(request, "success", f"Member removed: {member_email}. The mailbox can be added again later.")
    return redirect(f"/warm?cluster_id={cluster_id}")


@app.post("/warm/clusters/members/clear-blacklisted")
async def clear_blacklisted_warm_members_route(request: Request, cluster_id: str = Form("")):
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")
    cluster_id = cluster_id.strip() or request.session.get("warm_cluster_id") or ""
    cluster = get_warm_cluster(cluster_id, include_secrets=True)
    if not cluster or cluster.get("role") != "owner":
        flash(request, "error", "Only the Cluster Owner can clear blacklisted members.")
        return redirect("/warm")
    if not cluster.get("owner_private_key"):
        flash(request, "error", "This machine does not have the Owner Key needed to clear members.")
        return redirect(f"/warm?cluster_id={cluster_id}")

    blacklisted_members = [
        row for row in list_warm_cluster_members(cluster_id)
        if (row.get("status") or "").strip().lower() == "blacklisted"
    ]
    if not blacklisted_members:
        flash(request, "info", "No blacklisted members to clear.")
        return redirect(f"/warm?cluster_id={cluster_id}")

    cleared = []
    failed = []
    for row in blacklisted_members:
        email = normalize_email(row.get("email", ""))
        if not email:
            continue
        try:
            signature = make_owner_signature(cluster.get("owner_private_key", ""), cluster_id, "remove", email)
        except Exception as exc:
            failed.append(f"{email}: unable to sign owner action ({exc})")
            continue
        try:
            remove_warm_cluster_member(auth_data["access_token"], cluster_id, email, signature)
        except WarmApiError as exc:
            error_text = str(exc)
            if "not found" not in error_text.lower():
                failed.append(f"{email}: {error_text}")
                continue
        delete_warm_cluster_member(cluster_id, email)
        delete_warm_mailbox(email, cluster_id)
        log_warm_event(cluster_id=cluster_id, mailbox_email=email, event_type="member_blacklist_cleared", status="removed")
        cleared.append(email)

    if cleared:
        flash(request, "success", f"Cleared {len(cleared)} blacklisted member(s): {', '.join(cleared[:5])}.")
    if failed:
        flash(request, "warning", f"{len(failed)} blacklisted member(s) could not be cleared. {'; '.join(failed[:3])}")
    if not cleared and not failed:
        flash(request, "info", "No valid blacklisted members were found.")
    return redirect(f"/warm?cluster_id={cluster_id}")


@app.post("/warm/content/preview")
async def warm_content_preview_route(
    request: Request,
    cluster_id: str = Form(""),
    task_id: str = Form(""),
    provider: str = Form(""),
    stage: str = Form("initial_send"),
    topic: str = Form(""),
    use_llm: str = Form("0"),
):
    cluster_id = cluster_id.strip() or request.session.get("warm_cluster_id") or ""
    preview = generate_warm_content(
        task_id=task_id.strip() or f"preview-{int(time.time())}",
        cluster_id=cluster_id,
        provider=provider.strip(),
        stage=stage,
        topic=topic,
        previous_messages=[],
        use_llm=use_llm == "1",
    )
    request.session["warm_content_preview"] = preview
    flash(request, "success", f"Warm content generated locally from {preview.get('source', 'template')}.")
    return redirect(f"/warm?cluster_id={cluster_id}" if cluster_id else "/warm")


@app.post("/warm/mailboxes/status")
async def set_warm_mailbox_status(
    request: Request,
    sender_email: str = Form(""),
    status: str = Form("paused"),
):
    lang = get_lang(request)
    email = normalize_email(sender_email)
    next_status = status if status in {"active", "paused"} else "paused"
    if email and update_warm_mailbox_status(email, next_status):
        log_warm_event(mailbox_email=email, event_type="mailbox_status", status=next_status)
        flash(request, "success", t(lang, "warm_paused", email=email))
    else:
        flash(request, "error", t(lang, "warm_sender_required"))
    return redirect("/warm")


@app.post("/warm/mailboxes/reverify")
async def reverify_warm_mailbox_route(
    request: Request,
    cluster_id: str = Form(""),
    sender_email: str = Form(""),
):
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")
    email = normalize_email(sender_email)
    cluster_id = cluster_id.strip() or request.session.get("warm_cluster_id") or ""
    if not email or not cluster_id:
        flash(request, "error", "Choose a warm mailbox and cluster before re-verifying.")
        return redirect("/warm")
    sender = get_sender(email)
    if not sender or not sender_is_gmail_api_or_gmail(sender):
        flash(request, "error", "Reverify requires a saved Gmail API / Workspace sender.")
        return redirect(f"/warm?cluster_id={cluster_id}")
    verified = await verify_warm_mailbox_for_operation(request, auth_data, email)
    if not verified.get("ok"):
        flash(request, "error", verified.get("error", "Mailbox verification failed."))
        return redirect(f"/warm?cluster_id={cluster_id}")

    policy = warm_policy_config()
    local_mailbox = next(
        (row for row in list_warm_mailboxes() if normalize_email(row.get("email", "")) == email and row.get("cluster_id") == cluster_id),
        {},
    )
    daily_limit = int(local_mailbox.get("daily_limit") or 5)
    timezone = local_mailbox.get("timezone") or policy["timezone"]
    cluster = get_warm_cluster(cluster_id, include_secrets=True)
    payload = {
        "cluster_id": cluster_id,
        "owner_email": cluster.get("owner_email", ""),
        "email": email,
        "provider": local_mailbox.get("provider") or detect_provider(email),
        "status": "active",
        "capabilities": ["send", "scan", "reply", "inbox_rescue"],
        **_warm_remote_policy_payload(policy, daily_limit=daily_limit, timezone=timezone),
    }
    if cluster.get("owner_private_key"):
        payload.update(make_owner_signature(cluster.get("owner_private_key", ""), cluster_id, "approve", email))
    try:
        register_warm_mailbox(auth_data["access_token"], payload)
    except WarmApiError as exc:
        flash(request, "error", f"Warm policy sync failed after verification: {exc}")
        return redirect(f"/warm?cluster_id={cluster_id}")

    _upsert_local_warm_mailbox(cluster_id, email, "active", daily_limit, timezone, policy)
    flash(request, "success", f"Warm mailbox re-verified and policy synced: {email}.")
    return redirect(f"/warm?cluster_id={cluster_id}")


@app.post("/warm/clusters/clear-local")
async def clear_local_warm_clusters_route(
    request: Request,
    cluster_id: str = Form(""),
    scope: str = Form("remote"),
):
    if (scope or "").strip().lower() == "local":
        deleted = clear_warm_cluster_state()
        request.session.pop("warm_cluster_id", None)
        flash(request, "success", f"Cleared {deleted} local warm cluster state record(s). Remote ePetrel clusters were not changed.")
        return redirect("/warm")

    cluster_id = cluster_id.strip() or request.session.get("warm_cluster_id") or ""
    auth_data = get_required_warm_auth(request)
    if not auth_data:
        return redirect("/warm")
    if not cluster_id:
        deleted = clear_warm_cluster_state()
        request.session.pop("warm_cluster_id", None)
        flash(request, "success", f"No warm cluster is selected. Cleared {deleted} local warm cluster state record(s). Remote ePetrel mailbox ownership was not changed.")
        return redirect("/warm")

    cluster = get_warm_cluster(cluster_id, include_secrets=True)
    is_owner_client = (cluster.get("role") or "").strip().lower() == "owner" or bool(cluster.get("owner_private_key"))
    payload = {"cluster_id": cluster_id}
    if is_owner_client and cluster.get("owner_private_key"):
        payload.update(make_owner_signature(cluster.get("owner_private_key", ""), cluster_id, "dissolve"))
        payload["owner_email"] = cluster.get("owner_email", "")
    try:
        if is_owner_client:
            dissolve_warm_cluster(auth_data["access_token"], cluster_id, payload)
        else:
            leave_warm_cluster(auth_data["access_token"], cluster_id, payload)
    except WarmApiError as exc:
        message = str(exc)
        if (not is_owner_client) and ("owner_must_dissolve" in message or "must dissolve" in message):
            try:
                dissolve_warm_cluster(auth_data["access_token"], cluster_id, {"cluster_id": cluster_id})
            except WarmApiError as dissolve_exc:
                if _warm_error_is_cluster_dissolved(dissolve_exc):
                    mark_warm_cluster_dissolved(cluster_id)
                    deleted = clear_warm_cluster_state()
                    request.session.pop("warm_cluster_id", None)
                    flash(request, "success", f"Warm cluster was already dissolved; cleared {deleted} local warm state record(s).")
                    return redirect("/warm")
                flash(request, "error", f"Warm cluster dissolve failed: {dissolve_exc}. Use Clear Local Only if you only need to reset this client.")
                return redirect(f"/warm?cluster_id={cluster_id}")
            deleted = clear_warm_cluster_state()
            request.session.pop("warm_cluster_id", None)
            flash(request, "success", f"Warm cluster dissolved and local state cleared. Cleared {deleted} local warm state record(s).")
            return redirect("/warm")
        if _warm_error_is_cluster_dissolved(exc):
            mark_warm_cluster_dissolved(cluster_id)
            deleted = clear_warm_cluster_state()
            request.session.pop("warm_cluster_id", None)
            flash(request, "success", f"Warm cluster was already dissolved; cleared {deleted} local warm state record(s).")
            return redirect("/warm")
        action = "dissolve" if is_owner_client else "leave"
        flash(request, "error", f"Warm cluster {action} failed: {exc}. Use Clear Local Only if you only need to reset this client.")
        return redirect(f"/warm?cluster_id={cluster_id}")

    deleted = clear_warm_cluster_state()
    request.session.pop("warm_cluster_id", None)
    if is_owner_client:
        flash(request, "success", f"Warm cluster dissolved and local state cleared. Cleared {deleted} local warm state record(s).")
    else:
        flash(request, "success", f"Left warm cluster and local state cleared. Your local warm mailboxes were paused. 已退出 Warm 群并清空本地状态，本地 Warm 邮箱已暂停。 Cleared {deleted} local warm state record(s).")
    return redirect("/warm")


@app.post("/warm/mailboxes/clear-local")
async def clear_local_warm_mailboxes_route(request: Request):
    deleted = clear_warm_mailboxes()
    flash(request, "success", f"Cleared {deleted} local warm mailbox record(s). Remote ePetrel members were not deleted.")
    return redirect("/warm")



@app.post("/llm")
async def save_llm(
    request: Request,
    provider: str = Form("openai"),
    scope: str = Form("warm"),
    api_key: str = Form(""),
    base_url: str = Form(""),
    model: str = Form(""),
    system_prompt: str = Form(""),
):
    lang = get_lang(request)
    base_provider = provider.replace("warm_", "")
    if scope != "warm" or base_provider != "openai":
        flash(request, "error", "MutualWarm only supports the Warm OpenAI-compatible LLM configuration.")
        return redirect("/config")
    upsert_llm_settings("warm_openai", api_key=api_key, base_url=base_url, model=model, system_prompt=system_prompt, status="active")
    flash(request, "success", t(lang, "llm_saved"))
    return redirect("/config")


@app.get("/llm", include_in_schema=False)
async def llm_page_removed():
    return JSONResponse({"detail": "Not Found"}, status_code=404)


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    warm_provider_settings = get_llm_settings("warm_openai") or {}
    proxy_settings = get_proxy_settings()
    senders = list_senders()
    return templates.TemplateResponse(
        request=request,
        name="config.html",
        context=page_context(
            request,
            "config",
            "config_title",
            "config_caption",
            senders=senders,
            gmail_oauth_authorization_url=request.session.pop("gmail_oauth_authorization_url", ""),
            gmail_consumer_daily_limit=GMAIL_ACCOUNT_TYPE_DAILY_LIMITS["consumer_gmail"],
            gmail_workspace_daily_limit=GMAIL_ACCOUNT_TYPE_DAILY_LIMITS["workspace_gmail"],
            available_sender_count=len(senders),
            warm_provider_settings=warm_provider_settings,
            warm_default_base_url=OPENAI_BASE_URL,
            warm_default_model="gpt-4o-mini",
            warm_default_system_prompt=WARM_LLM_SYSTEM_PROMPT,
            proxy_settings=proxy_settings,
        ),
    )
