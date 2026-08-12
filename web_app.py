import asyncio
import hashlib
import os
import random
import re
import sqlite3
import time
import json
import logging
import uuid
from urllib.parse import urlencode
from html import escape
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import (
    ANTHROPIC_BASE_URL,
    ANTHROPIC_MODEL,
    BOUNCE_RATE_ALERT,
    DB_PATH,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_SYSTEM_PROMPT,
    EMAIL_TEST_POLL_INTERVAL_SECONDS,
    EMAIL_TEST_POLL_SECONDS,
    EPETREL_SITE_URL,
    HARD_BOUNCE_RATE_ALERT,
    MAILFORGE_IMAP_HOST,
    MAILFORGE_IMAP_PORT,
    MAILFORGE_SMTP_HOST,
    MAILFORGE_SMTP_PORT,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    REMARKETING_COOLDOWN_DAYS,
    SPAM_PLACEMENT_RATE_ALERT,
    UNSUBSCRIBE_RATE_ALERT,
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
    CRM_DEFAULT_REMARKETING_MAX,
    CRM_HARD_REMARKETING_MAX,
    CRM_STATUSES,
    abandon_due_crm_contacts,
    add_crm_tags,
    append_crm_note,
    can_run_email_test_for_domain,
    clear_delivery_events,
    clear_outbound_logs,
    clear_seed_accounts,
    clear_senders,
    clear_warm_cluster_state,
    clear_warm_mailboxes,
    delete_outbound_log,
    delete_warm_cluster_member,
    delete_warm_mailbox,
    delete_sender,
    delete_email_template,
    crm_contact_auto_excluded,
    crm_dashboard_summary,
    crm_funnel_stats,
    get_app_setting,
    get_secret_app_setting,
    get_crm_contact,
    get_crm_timeline,
    get_email_template,
    get_llm_settings,
    get_remarketing_template,
    get_sender,
    get_warm_cluster,
    get_warm_summary,
    init_db,
    increment_email_test_domain_count,
    keep_only_warm_cluster,
    list_recent_successful_receivers,
    list_successful_receivers,
    list_llm_settings,
    list_email_templates,
    list_crm_contacts,
    list_crm_contacts_for_export,
    list_crm_tags,
    list_crm_tasks,
    list_external_touch_queue,
    list_remarketing_candidates,
    list_remarketing_templates,
    list_seed_accounts,
    list_senders,
    list_warm_mailboxes,
    list_warm_cluster_members,
    list_warm_clusters,
    mark_warm_cluster_dissolved,
    log_outbound,
    log_crm_activity,
    log_warm_event,
    mark_crm_external_touch,
    mark_crm_outbound,
    remove_crm_tags,
    set_crm_contact_status,
    set_crm_next_followup,
    update_crm_channels,
    upsert_crm_contact,
    upsert_crm_task,
    upsert_email_template,
    upsert_llm_settings,
    upsert_remarketing_template,
    upsert_app_setting,
    upsert_secret_app_setting,
    upsert_seed_account,
    upsert_sender,
    upsert_warm_cluster,
    upsert_warm_cluster_member,
    upsert_warm_mailbox,
    update_warm_cluster_member_status,
    update_warm_mailbox_status,
    WARM_LLM_SYSTEM_PROMPT,
)
from modules.ai_agent import generate_copy_variants, generate_icebreaker
from modules.deliverability import COLD_EMAIL_WORD_MAX, COLD_EMAIL_WORD_MIN, analyze_email_locally, lint_email, load_dangerous_words
from modules.email_engine import (
    calculate_dispatch_delay,
    get_active_senders,
    get_domain,
    html_to_plain_text,
    normalize_email,
    send_cold_email,
)
from modules.email_test_service import (
    EmailTestApiError,
    analyze_email_deliverability,
    create_email_test_request,
    diagnose_email_test_gmail,
    poll_email_deliverability_analysis,
    poll_email_test_request,
)
from modules.gmail_api import (
    GMAIL_BASE_SCOPES,
    GMAIL_FULL_AUTO_WARM_SCOPES,
    GMAIL_MODIFY_SCOPE,
    build_gmail_oauth_url,
    exchange_gmail_oauth_code,
    fetch_gmail_profile,
)
from modules.imap_worker import fetch_all_inboxes
from modules.safe_logging import configure_file_logger, mask_email, redact_sensitive
from modules.seed_monitor import check_all_seed_accounts
from modules.sender_checks import check_imap_login, check_sender_mailbox
from modules.spintax_parser import parse_spintax
from modules.warm_account_probe import (
    GMAIL_MODIFY_SETUP_HINT,
    move_warm_account_probe_to_inbox,
    scan_warm_account_probe,
    send_warm_account_probe_reply,
    warm_inbox_rescue_capability,
)
from modules.warm_client import (
    WARM_RULES,
    detect_provider,
    derive_owner_public_key,
    generate_cluster_id,
    generate_owner_keypair,
    generate_cluster_secret,
    make_owner_signature,
    next_human_reply_time,
    warm_policy_config,
)
from modules.network_proxy import apply_proxy_settings, get_proxy_settings, save_proxy_settings
from modules.warm_content import WARM_CONTENT_STAGES, WARM_TOPICS, generate_warm_content, warm_llm_self_check
from modules.warm_worker import set_warm_worker_auth, start_warm_worker, stop_warm_worker
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
    report_warm_mailbox_ownership_reply,
    remove_warm_cluster_member,
    start_warm_mailbox_ownership,
    start_warm_auth,
    verify_warm_mailbox_ownership,
)

WARM_RULE_CARD_META = [
    {"icon": "lock", "tone": "primary"},
    {"icon": "shield_person", "tone": "success"},
    {"icon": "person_check", "tone": "primary"},
    {"icon": "link_off", "tone": "danger"},
    {"icon": "warning", "tone": "warning"},
    {"icon": "card_giftcard", "tone": "success"},
    {"icon": "visibility_off", "tone": "muted", "wide": True},
]


init_db()
apply_proxy_settings()

app = FastAPI(title="MutualWarm")
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


PAGE_KEYS = ["warm", "config"]
LANGUAGE_LABELS = {"en": "English", "zh": "中文"}

TEXT = {
    "en": {
        "app_name": "MutualWarm",
        "app_subtitle": "Open Source Warm Client",
        "system_status": "System Operational",
        "language": "Language",
        "deploy": "Deploy",
        "nav_title": "Workspace",
        "page.warm": "Warm Network",
        "page.config": "Configuration",
        "dispatch_title": "Cold Email Dispatch Control",
        "dispatch_caption": "Mailbox rotation, Mail SMTP, spintax, AI icebreakers, throttling, and unsubscribe protection.",
        "warm_title": "MutualWarm Network",
        "warm_caption": "Decentralized inbox placement, delayed replies, and contribution tracking for opted-in sender mailboxes.",
        "config_title": "Configuration",
        "config_caption": "Configure sender mailboxes, Gmail API OAuth, and the Warm OpenAI-compatible LLM used by MutualWarm.",
        "proxy_settings": "HTTP(S) Proxy Settings",
        "proxy_settings_caption": "Optional proxy for Google OAuth/API, ePetrel services, and LLM HTTP(S) requests.",
        "proxy_enabled": "Use proxy / 启用代理",
        "proxy_enabled_hint": "When off, this client does not apply the proxy configured here.",
        "proxy_address": "Proxy address",
        "proxy_address_placeholder": "http://127.0.0.1:7890",
        "proxy_save": "Save Proxy Settings",
        "proxy_saved": "Proxy settings saved.",
        "proxy_invalid": "Proxy settings were not saved: {error}",
        "proxy_note_title": "How to use / 使用说明",
        "proxy_note": "EN: 127.0.0.1 means the proxy is running on this computer, so the host is usually the same for every local user. The port is not universal: enter the port shown by your proxy app (for example 7890, 7897, 1080, or another value). If the proxy runs on another computer, replace 127.0.0.1 with that computer's host or IP.\n中文：127.0.0.1 表示代理运行在本机，因此本机代理的地址通常都一样；但端口不是所有用户都相同，请填写代理软件显示的端口（例如 7890、7897、1080 或其他端口）。如果代理运行在另一台电脑，请将 127.0.0.1 改成那台电脑的主机名或 IP。",
        "proxy_scope_note": "EN: This setting applies to HTTP(S) traffic such as Google OAuth, Gmail API, ePetrel BFF, and the LLM endpoint. It does not proxy SMTP/IMAP raw socket connections.\n中文：此设置用于 Google OAuth、Gmail API、ePetrel BFF 和 LLM 接口等 HTTP(S) 请求；不会代理 SMTP/IMAP 原始 socket 连接。",
        "crm_title": "CRM Workspace",
        "crm_caption": "Manage replies, remarketing, lead details, tags, tasks, external touch queues, and Obsidian graph export.",
        "crm_saved": "CRM updated.",
        "crm_bulk_done": "Bulk action updated {count} contacts.",
        "crm_contact_missing": "CRM contact was not found.",
        "crm_template_saved": "Remarketing template {step} saved.",
        "crm_remarketing_done": "Remarketing sent {sent}, skipped {skipped}, failed {failed}.",
        "crm_obsidian_exported": "Exported {count} CRM notes to {path}.",
        "crm_import_done": "Imported {count} contacts into CRM.",
        "crm_api_ok": "CRM API update saved.",
        "warm_enabled": "Warm mailbox enabled: {email}.",
        "warm_paused": "Warm mailbox status updated: {email}.",
        "warm_invalid_domain": "This mailbox domain is not allowed for Warm Network. Allowed domains are managed by ePetrel policy.",
        "warm_sender_required": "Choose a saved sender mailbox before enabling Warm Network.",
        "sender_pool": "Mail Sender Pool",
        "sender_email": "Email",
        "sender_password": "Password / App Password",
        "auth_method": "Send Method",
        "auth_method_smtp": "SMTP / App Password",
        "auth_method_gmail_api": "Gmail API OAuth",
        "gmail_account_type": "Gmail Account Type",
        "gmail_account_type_consumer": "Personal Gmail",
        "gmail_account_type_workspace": "Workspace Gmail",
        "gmail_oauth_link": "Gmail OAuth Link",
        "copy_gmail_oauth_link": "Generate OAuth Link",
        "gmail_sender_oauth_hint": "Enter the OAuth Client ID and Client Secret for this exact sender every time you generate an OAuth link or connect Gmail API. The values are saved only to this sender after authorization succeeds.",
        "open_in_profile_hint": "Use the generated link in the browser profile for this exact mailbox. This prevents account-selection mistakes; it is not a risk-control bypass.",
        "gmail_oauth_link_ready": "OAuth link generated for {email}. Copy it into the browser profile where that exact mailbox is signed in.",
        "gmail_oauth_email_mismatch": "Google authorized {actual}, but this sender is {expected}. Token was not saved; reconnect with the correct Gmail account.",
        "gmail_client_id": "Gmail OAuth Client ID",
        "gmail_client_secret": "Gmail OAuth Client Secret",
        "gmail_client_secret_saved": "Saved locally",
        "gmail_modify_scope": "Request Gmail read/rescue scopes for Warm",
        "gmail_modify_scope_hint": "Gmail API OAuth explicitly requests gmail.send, gmail.readonly, and gmail.modify so MutualWarm sending, scanning, and rescue workflows can run. Historical Google grants are not merged.",
        "connect_gmail_api": "Connect Gmail API",
        "gmail_api_hint": "Gmail API OAuth explicitly requests gmail.send, gmail.readonly, and gmail.modify, with include_granted_scopes=false. This avoids Windows OAuth scope-change errors from older Google grants while keeping Warm features available.",
        "gmail_api_missing_config": "Enter a valid Gmail address, From Name, daily limit, and Gmail OAuth Client ID / Client Secret. If a Client Secret is saved locally, you can leave it blank.",
        "gmail_api_connected": "Gmail API OAuth connected for {email}.",
        "gmail_api_connected_limited": "Gmail API OAuth connected for {email}, but Google did not grant all Warm scopes. Check OAuth consent screen scopes and reconnect this sender.",
        "gmail_api_failed": "Gmail API OAuth failed: {error}",
        "daily_limit": "Daily Limit",
        "from_name": "From Name",
        "save_sender": "Save Sender",
        "delete_sender": "Delete",
        "deleted_sender": "Deleted sender mailbox {email}.",
        "delete_sender_missing": "Sender mailbox was not found.",
        "delete_sender_confirm": "Delete this sender mailbox?",
        "clear_senders": "Clear Senders",
        "clear_senders_confirm": "Clear all sender mailboxes from this local client?",
        "cleared_senders": "Cleared {count} sender mailboxes.",
        "import_senders": "Import Senders",
        "sender_import_file": "Sender Excel / CSV",
        "sender_import_hint": "Required columns: Email, Password, Daily Limit, From Name, SMTP Host, SMTP Port, IMAP Host, IMAP Port. Host and port values must be filled for every row.",
        "sender_template": "Download sender template",
        "sender_provider_hint": "Provider reference for common Gmail / Workspace and Outlook / Microsoft 365 mailboxes. SMTP uses 16-character app passwords when the provider requires 2-step verification; Gmail API OAuth is available for Gmail sending.",
        "smtp_host": "SMTP Host",
        "smtp_port": "SMTP Port",
        "email_security": "Security",
        "sender_import_check": "Run SMTP/IMAP login checks after import",
        "sender_import_uploading": "Uploading and saving sender mailboxes...",
        "sender_import_checking": "Importing and checking SMTP/IMAP login. This can take a few minutes; keep this page open.",
        "sender_import_missing_file": "Upload an .xlsx or .csv sender file.",
        "sender_import_missing_cols": "The sender file must include Email, Password, Daily Limit, From Name, SMTP Host, SMTP Port, IMAP Host, and IMAP Port columns.",
        "sender_import_missing_required": "All required sender fields must be filled.",
        "sender_import_done": "Imported {count} sender mailboxes. Failed rows: {failed}.",
        "sender_import_row_error": "Row {row}: {error}",
        "no_senders": "No sender mailbox has been configured.",
        "valid_sender_error": "Enter a valid sender email, password, daily limit, from name, SMTP host/port, and IMAP host/port.",
        "saved_sender": "Saved {email}",
        "sender_check_passed": "SMTP and IMAP login passed. Mailbox appears active.",
        "sender_check_failed": "Saved {email}, but mailbox login check failed: {error}",
        "smtp_check": "SMTP Check",
        "imap_check": "IMAP Check",
        "mailbox_check": "Mailbox Check",
        "email_test_title": "Sender Score Check",
        "email_test_caption": "Analyze one random sender per domain plus the current subject/body template. ePetrel adds DNS, authentication, and reputation checks after login.",
        "email_test_start_auth": "Log in to ePetrel",
        "email_test_authorize_refresh": "Authorize",
        "email_test_open_auth": "Open ePetrel Signup / Login",
        "email_test_check_auth": "Check Authorization",
        "email_test_auth_pending": 'Please click the "Authorize" button to log in.',
        "email_test_auth_stalled": "Authorization is still pending because ePetrel has not confirmed it yet. Please try again.",
        "email_test_authorized": "Logged in to ePetrel.",
        "email_test_sender": "Sender Under Test",
        "email_test_subject": "Template Under Test",
        "email_test_wait": "Wait for result",
        "email_test_send": "Analyze Template and Domains",
        "email_test_poll": "Refresh All Placement Results",
        "email_test_no_auth": "Authorize with ePetrel before sending a managed Gmail placement test.",
        "email_test_no_sender": "Add an active sender mailbox before running the placement test.",
        "email_test_sent": "Generated {count} deliverability analysis reports.",
        "email_test_domain_limited": "{domain} has already used {used}/3 deliverability analyses today.",
        "email_test_status": "Request {request_id}: {status}",
        "email_test_result": "Placement result: {placement}",
        "email_test_pending_help": "",
        "email_test_progress": "Submitting analysis request...",
        "email_test_domain": "Domain: {domain}",
        "email_test_diagnose": "Diagnose Gmail API",
        "email_test_diagnostics_title": "Gmail API diagnostics",
        "email_test_diagnostics_empty": "No Gmail API diagnostic has been run yet.",
        "email_test_diagnostics_running": "Running Gmail API diagnostics. This can take up to 25 seconds.",
        "email_test_diagnostics_ok": "Gmail API is reachable. Pending: {pending}; recent completed: {completed}; scan checked {checked} messages and matched {matched}.",
        "email_test_diagnostics_fail": "Gmail API diagnostic failed: {error}",
        "email_test_auto_poll_paused": "Auto-refresh is paused briefly so you can read the diagnostic result.",
        "email_test_sender_status": "{sender}: {status}",
        "email_test_error": "Email test failed: {error}",
        "email_test_reset": "Reset Authorization",
        "email_test_register_hint": "Need an account first? Use ePetrel signup at {url}.",
        "email_test_report_title": "Deliverability Report",
        "email_test_report_caption": "Merged report from local template checks and ePetrel backend domain checks.",
        "email_test_report_empty": "Run an authorized analysis to see score, categories, risk words, and fixes here.",
        "email_test_report_pending": "ePetrel is checking DNS, authentication, and reputation. This page refreshes automatically.",
        "email_test_report_overall": "Overall Score",
        "email_test_report_risk_words": "Risk words",
        "email_test_report_no_risk_words": "No risk words detected.",
        "email_test_report_findings": "Findings",
        "email_test_report_no_findings": "No major issue in this category.",
        "email_test_backend_error": "Backend analysis failed: {error}",
        "email_test_analysis_queued": "Analysis queued. The report will refresh automatically.",
        "email_test_analysis_status": "Analysis status: {status}",
        "template_risk_preview": "Risk Highlight Preview",
        "template_risk_preview_note": "Risk words and links are highlighted here while you edit the template. Content findings are not repeated in the domain report.",
        "report_prev": "Previous",
        "report_next": "Next",
        "report_page": "Page {page} / {pages}",
        "load_leads": "Load Target Leads",
        "lead_uploader": "Supports .csv / .xlsx. The file must include an Email column.",
        "lead_file": "Lead CSV / Excel",
        "preview_leads": "Preview Leads",
        "lead_preview_title": "Lead Preview",
        "lead_preview_empty": "Upload a lead file to validate the Email column and preview recipients.",
        "lead_preview_done": "Lead file looks ready: {rows} rows, {valid} valid email addresses.",
        "lead_preview_filename": "File: {filename}",
        "lead_preview_page": "Page {page} / {pages}",
        "lead_send_status": "Send Status",
        "lead_sent": "Sent",
        "lead_unsent": "Not sent",
        "lead_status": "Status",
        "lead_status_valid": "Valid",
        "lead_status_invalid": "Invalid",
        "lead_actions": "Actions",
        "delete_lead": "Delete lead",
        "delete_lead_confirm": "Remove this lead from the current preview list?",
        "deleted_lead": "Removed lead row {row}.",
        "clear_leads": "Clear Leads",
        "clear_leads_confirm": "Clear the current lead preview list?",
        "cleared_leads": "Cleared the current lead preview list.",
        "lead_preview_missing": "Preview a lead file before deleting rows.",
        "lead_file_missing": "Upload a .csv or .xlsx lead file before previewing.",
        "lead_file_unsupported": "Lead file must be .csv or .xlsx.",
        "lead_no_valid": "The lead list does not include any valid email addresses.",
        "remarketing_cooldown_label": "Remarketing Cooldown",
        "remarketing_cooldown_hint": "Contacts successfully sent within this many days are skipped. Set 0 to allow immediate remarketing.",
        "remarketing_cooldown_saved": "Saved",
        "lead_cleaning_hint": "Before uploading, verify the list with UseBouncer or a similar email verification tool to reduce bounces and protect sender reputation.",
        "custom_fields_hint": "Any uploaded column can be used as a variable in the subject or body, such as {Name}, {Company}, {Company_Bio}, {Position}, or your own custom column names.",
        "dispatch_progress_audit_hint": "For detailed sending progress, keep this page open and view Audit Logs in a new browser tab.",
        "missing_email_col": "The lead list is missing an Email column.",
        "loaded_leads": "Loaded {rows} rows, with {valid} valid email addresses.",
        "content_config": "Configure Copy Variants",
        "subject": "Subject",
        "html_body": "Body / Spintax Variants",
        "unsubscribe_copy": "Unsubscribe Line",
        "unsubscribe_placeholder": "Example only: Not interested? Just reply no.",
        "signature": "Signature",
        "signature_placeholder": "BR\nSender name\nTitle, Company",
        "save_unsubscribe_copy": "Save unsubscribe line",
        "save_signature": "Save signature",
        "unsubscribe_copy_saved": "Unsubscribe line saved for future templates.",
        "signature_saved": "Signature saved for future templates.",
        "template_library": "Template Library",
        "template_slot": "Template {slot}",
        "template_empty": "Empty slot",
        "template_name": "Template name",
        "template_load": "Load",
        "template_save_current": "Save current",
        "template_delete": "Delete",
        "template_expand": "Show all",
        "template_collapse": "Show one",
        "template_saved": "Email template {slot} saved.",
        "template_loaded": "Email template {slot} loaded.",
        "template_deleted": "Email template {slot} deleted.",
        "template_missing": "This template slot is empty.",
        "template_save_confirm": "Overwrite this saved template?",
        "template_delete_confirm": "Delete this saved template?",
        "word_count_title": "Cold email length",
        "word_count_status": "__COUNT__ words. Recommended range: __MIN__-__MAX__ words.",
        "generate_variants": "AI Optimize & Vary Copy",
        "progress_ai_variant_title": "Optimizing with LLM",
        "progress_ai_variant_text": "Generating safer copy variants. This can take a few seconds.",
        "progress_sender_check_title": "Checking sender mailbox",
        "progress_sender_check_text": "Testing SMTP and IMAP login before saving this sender.",
        "progress_gmail_oauth_title": "Preparing Gmail OAuth",
        "progress_gmail_oauth_text": "Opening Google authorization and saving this sender after consent.",
        "progress_lead_preview_title": "Previewing lead file",
        "progress_lead_preview_text": "Reading the uploaded file and validating recipient emails.",
        "progress_inbox_sync_title": "Syncing inboxes",
        "progress_inbox_sync_text": "Fetching recent replies from saved sender mailboxes.",
        "progress_seed_sync_title": "Syncing seed placement",
        "progress_seed_sync_text": "Scanning seed inbox and spam folders for placement events.",
        "progress_llm_save_title": "Saving LLM settings",
        "progress_llm_save_text": "Encrypting the provider key and updating local model settings.",
        "variant_help": "Use variables like {Name}, {Company}, {Company_Bio}, and {Position}. You can write your own {variant A|variant B} Spintax, or use AI optimization to replace the current body with deliverability-aware variants.",
        "variant_action_hint": "Rewrites the body into a lower-risk one-to-one tone, removes spammy phrasing, preserves variables and links, and adds safe Spintax variation.",
        "variant_format_error": "Copy variant format has an unmatched brace or empty Spintax option.",
        "template_variable_missing_cols": "Dispatch blocked: template variables are missing from the lead file: {columns}. Add these columns or remove the variables.",
        "template_variable_empty_values": "Dispatch blocked: template variables are empty in these lead rows: {details}. Fill them or delete those rows before sending.",
        "variant_generated": "AI optimized the copy and replaced it with deliverability-aware variants.",
        "variant_generate_failed": "AI did not return a valid optimized Spintax version. Try again, or simplify the body while keeping variables intact.",
        "reputation_ps_hint": "The preview combines body, unsubscribe line, and signature. The unsubscribe and signature fields remain editable and are checked for risk words and links.",
        "queue_control": "Flow Control",
        "delay_min": "Min Delay (s)",
        "delay_max": "Max Delay (s)",
        "use_ai": "AI realtime icebreaker",
        "variant": "Variant Tag",
        "mix_seed": "Mix seed test inboxes",
        "seed_interval": "Seed interval",
        "start_queue": "Start Dispatch Queue",
        "available_senders": "Available senders: {count}",
        "batch_done": "Congratulations, the dispatch queue completed successfully.",
        "dispatch_working_title": "Dispatch queue is running",
        "dispatch_working_body": "Sending is in progress. Keep this page open; sent badges update automatically.",
        "dispatch_stop": "Stop",
        "dispatch_stopping": "Stopping...",
        "dispatch_stop_requested": "Stop requested. The queue will stop after the current send or delay.",
        "dispatch_stopped": "Dispatch queue stopped by user.",
        "security_title": "Sender Safety Monitor",
        "security_caption": "Based on local send logs, IMAP bounce parsing, unsubscribe recognition, and seed inbox sampling.",
        "seed_pool": "Seed Test Inbox Pool",
        "no_seeds": "No seed test inbox has been configured.",
        "seed_email": "Seed Email",
        "seed_password": "IMAP Password / App Password",
        "provider": "Provider",
        "imap_host": "IMAP Host",
        "imap_port": "IMAP Port",
        "inbox_folder": "Inbox Folder",
        "spam_folder": "Spam/Junk Folder",
        "status": "Status",
        "save_seed": "Save Seed Inbox",
        "saved_seed": "Saved seed inbox {email}",
        "valid_seed_error": "Enter a valid seed email, password, and IMAP host.",
        "days_window": "Stats Window / Days",
        "seed_limit": "Emails scanned per seed folder",
        "sync_seed": "Sync Seed Placement Now",
        "no_active_seed": "There is no active seed inbox.",
        "seed_sync_success": "{seed}: matched {matched}, missing events added {missing}",
        "clear_seeds": "Clear Seeds",
        "clear_seeds_confirm": "Clear all seed test inboxes from this local client?",
        "cleared_seeds": "Cleared {count} seed test inboxes.",
        "clear_security_outbound": "Clear Outbound",
        "clear_security_outbound_confirm": "Clear outbound summary data? Sender daily counts will be recalculated from remaining audit records.",
        "cleared_security_outbound": "Cleared {count} outbound records.",
        "clear_security_events": "Clear Events",
        "clear_security_events_confirm": "Clear all safety event history?",
        "cleared_security_events": "Cleared {count} safety events.",
        "metric_sent": "Successful Sends",
        "metric_failed": "SMTP Failed",
        "metric_bounce": "Bounce Rate",
        "metric_hard": "Hard Bounce",
        "metric_unsub": "Unsubscribe Rate",
        "metric_spam": "Seed Spam",
        "no_alerts": "No safety threshold was triggered in the current window.",
        "sender_domain_summary": "Sender / Domain Summary",
        "event_details": "Event Details",
        "sender_health": "Sender Health",
        "audit_title": "Historical Dispatch Audit",
        "audit_caption": "Review raw HTML, status, failure reason, and Message-ID.",
        "audit_actions": "Actions",
        "audit_error": "Reason",
        "audit_filter_status": "Status",
        "audit_filter_all": "All",
        "audit_filter_success": "Success",
        "audit_filter_failed": "Failed",
        "audit_filter_skipped": "Skipped",
        "audit_filter_unsent": "Failed + skipped",
        "audit_filter_sender": "Sender",
        "audit_filter_receiver": "Receiver",
        "audit_filter_domain": "Domain",
        "audit_filter_error": "Reason contains",
        "audit_apply_filters": "Apply filters",
        "audit_reset_filters": "Reset",
        "audit_export_unsent": "Export unsent receivers",
        "audit_export_empty": "No failed or skipped receiver emails match the current filters.",
        "audit_delete": "Delete log",
        "audit_delete_confirm": "Delete this audit log? Sender daily count will be recalculated from remaining successful audit records.",
        "audit_deleted": "Deleted audit log #{id}. Sender counts were recalculated from audit records.",
        "audit_delete_missing": "Audit log was not found.",
        "audit_clear": "Clear audit logs",
        "audit_clear_confirm": "Clear all dispatch audit logs? Sender daily counts will be recalculated from remaining audit records.",
        "audit_cleared": "Cleared {count} audit logs. Sender counts were recalculated from audit records.",
        "raw_trace": "Raw Email Render Trace",
        "select_email_id": "Email ID to inspect",
        "fetch_html": "Fetch Raw HTML",
        "not_found": "No email record was found for that ID.",
        "inbox_title": "Unified Shared Inbox",
        "inbox_caption": "Aggregate Mail replies and classify unsubscribe, refusal, and high-intent messages.",
        "fetch_limit": "Recent emails per mailbox",
        "sync_inbox": "Sync Inbox Now",
        "inbox_sync_success": "{sender}: stored {stored} new emails",
        "empty_inbox": "No customer replies yet.",
        "llm_title": "LLM Provider Settings",
        "llm_caption": "Store API keys securely, choose OpenAI-compatible endpoints or Anthropic Claude, and tune the default system prompt.",
        "active_provider": "Protocol",
        "api_key": "API Key",
        "base_url": "Base URL",
        "model": "Model",
        "system_prompt": "System Prompt",
        "save_llm": "Save Settings",
        "llm_saved": "LLM settings saved.",
        "llm_missing_key": "This provider has no API key yet. AI features will use fallback copy until a key is saved.",
        "current_llm": "Current LLM Configuration",
        "warm_current_llm": "Warm LLM Configuration",
        "cold_llm_caption": "Use the strongest model for cold-email copy, personalization, and deliverability-aware variants.",
        "warm_llm_caption": "Use a low-cost model for short, natural warm mailbox conversations.",
        "save_warm_llm": "Save Warm LLM",
        "toolkit": "Provider Toolkit",
        "openai_toolkit": "OpenAI / OpenAI-compatible protocol uses Chat Completions. Use this for OpenAI, DeepSeek, or other providers that expose an OpenAI-compatible endpoint: set the provider API key, Base URL, and exact model name from that provider.",
        "anthropic_toolkit": "Anthropic Claude uses the official Messages API: system is a top-level field, user content is sent in messages, and max_tokens is required.",
        "security_note": "Security: API keys use password inputs, are never rendered in tables, are masked after save, and are stored encrypted locally when cryptography is installed.",
        "system_prompt_help": "This system prompt guides AI icebreakers and copy variant generation. When editing it, keep strict instructions to preserve merge variables and output valid Spintax only; accidental changes can break personalization or sending format.",
    },
    "zh": {
        "app_name": "ePetrel AI",
        "app_subtitle": "群发系统",
        "system_status": "系统运行正常",
        "language": "语言",
        "deploy": "部署",
        "nav_title": "功能工作区",
        "page.warm": "Warm 网络",
        "page.config": "Configuration",
        "dispatch_title": "冷发信自动化控制台",
        "dispatch_caption": "多发件箱轮询、Mail SMTP、Spintax、AI 破冰、限额与退订抑制。",
        "warm_title": "MutualWarm 网络",
        "warm_caption": "为主动加入的发件箱提供去中心化落箱统计、延迟回复和贡献记录。",
        "proxy_settings": "HTTP(S) 代理设置",
        "proxy_settings_caption": "可选代理，用于 Google OAuth/API、ePetrel 服务和 LLM 的 HTTP(S) 请求。",
        "proxy_enabled": "Use proxy / 启用代理",
        "proxy_enabled_hint": "关闭后，客户端不会应用此处配置的代理。",
        "proxy_address": "代理地址 / Proxy address",
        "proxy_address_placeholder": "http://127.0.0.1:7890",
        "proxy_save": "保存代理设置 / Save Proxy Settings",
        "proxy_saved": "代理设置已保存。",
        "proxy_invalid": "代理设置未保存：{error}",
        "proxy_note_title": "使用说明 / How to use",
        "proxy_note": "EN: 127.0.0.1 means the proxy is running on this computer, so the host is usually the same for every local user. The port is not universal: enter the port shown by your proxy app (for example 7890, 7897, 1080, or another value). If the proxy runs on another computer, replace 127.0.0.1 with that computer's host or IP.\n中文：127.0.0.1 表示代理运行在本机，因此本机代理的地址通常都一样；但端口不是所有用户都相同，请填写代理软件显示的端口（例如 7890、7897、1080 或其他端口）。如果代理运行在另一台电脑，请将 127.0.0.1 改成那台电脑的主机名或 IP。",
        "proxy_scope_note": "EN: This setting applies to HTTP(S) traffic such as Google OAuth, Gmail API, ePetrel BFF, and the LLM endpoint. It does not proxy SMTP/IMAP raw socket connections.\n中文：此设置用于 Google OAuth、Gmail API、ePetrel BFF 和 LLM 接口等 HTTP(S) 请求；不会代理 SMTP/IMAP 原始 socket 连接。",
        "crm_title": "CRM 工作台",
        "crm_caption": "管理回信、再营销、客户详情、标签、任务、外部触达队列和 Obsidian 知识图谱导出。",
        "crm_saved": "CRM 已更新。",
        "crm_bulk_done": "批量操作已更新 {count} 个联系人。",
        "crm_contact_missing": "未找到该 CRM 联系人。",
        "crm_template_saved": "再营销模板 {step} 已保存。",
        "crm_remarketing_done": "再营销完成：发送 {sent}，跳过 {skipped}，失败 {failed}。",
        "crm_obsidian_exported": "已导出 {count} 个 CRM 笔记到 {path}。",
        "crm_import_done": "已导入 {count} 个联系人到 CRM。",
        "crm_api_ok": "CRM API 更新已保存。",
        "warm_enabled": "已启用 Warm 邮箱：{email}。",
        "warm_paused": "Warm 邮箱状态已更新：{email}。",
        "warm_invalid_domain": "该邮箱域名暂不允许加入 Warm Network。允许域名由 ePetrel 策略配置。",
        "warm_sender_required": "请先选择已保存的发件箱再启用 Warm Network。",
        "sender_pool": "Mail 发件箱池",
        "sender_email": "邮箱",
        "sender_password": "密码 / App Password",
        "auth_method": "发信方式",
        "auth_method_smtp": "SMTP / 应用专用密码",
        "auth_method_gmail_api": "Gmail API OAuth",
        "gmail_account_type": "Gmail 账号类型",
        "gmail_account_type_consumer": "普通 Gmail",
        "gmail_account_type_workspace": "企业 Workspace Gmail",
        "gmail_oauth_link": "Gmail OAuth 授权链接",
        "copy_gmail_oauth_link": "生成授权链接",
        "gmail_sender_oauth_hint": "每次生成授权链接或连接 Gmail API 时，都需要输入当前 sender 自己的 OAuth Client ID 和 Client Secret。授权成功后，这组值只保存到这个 sender。",
        "open_in_profile_hint": "请把生成的链接复制到该邮箱对应的浏览器 Profile 中打开，用来避免选错账号；这不是风控规避功能。",
        "gmail_oauth_link_ready": "已为 {email} 生成 OAuth 链接。请复制到该邮箱对应的浏览器 Profile 中打开。",
        "gmail_oauth_email_mismatch": "Google 实际授权的是 {actual}，但当前 sender 是 {expected}。Token 未保存；请使用正确 Gmail 账号重新授权。",
        "gmail_client_id": "Gmail OAuth Client ID",
        "gmail_client_secret": "Gmail OAuth Client Secret",
        "gmail_client_secret_saved": "Saved locally",
        "gmail_modify_scope": "请求 Warm 所需的 Gmail 读信/捞信权限",
        "gmail_modify_scope_hint": "Gmail API OAuth 会显式请求 gmail.send、gmail.readonly、gmail.modify，保证 Dispatch 发信和 Warm Gmail 扫描/捞信都可用，同时不会合并 Google 历史授权。",
        "connect_gmail_api": "连接 Gmail API",
        "gmail_api_hint": "Gmail API OAuth 会显式请求 gmail.send、gmail.readonly、gmail.modify，并设置 include_granted_scopes=false。这样既避免 Windows 因历史授权合并导致 scope changed 报错，也保证 Warm 功能可用。",
        "gmail_api_missing_config": "请输入有效 Gmail 邮箱、发件人名、每日上限、Gmail OAuth Client ID / Client Secret。如果本地已保存 Client Secret，可以留空复用。",
        "gmail_api_connected": "已为 {email} 连接 Gmail API OAuth。",
        "gmail_api_connected_limited": "已为 {email} 连接 Gmail API OAuth，但 Google 没有授予完整 Warm 权限。请检查 OAuth consent screen 的 scopes 后重新授权该 sender。",
        "gmail_api_failed": "Gmail API OAuth 失败：{error}",
        "daily_limit": "每日上限",
        "from_name": "发件人名",
        "save_sender": "保存发件箱",
        "delete_sender": "删除",
        "deleted_sender": "已删除发件箱 {email}。",
        "delete_sender_missing": "未找到该发件箱。",
        "delete_sender_confirm": "确认删除这个发件箱吗？",
        "clear_senders": "清空发件箱",
        "clear_senders_confirm": "确认清空这个本地客户端中的全部发件箱吗？",
        "cleared_senders": "已清空 {count} 个发件箱。",
        "import_senders": "导入发件箱",
        "sender_import_file": "发件箱 Excel / CSV",
        "sender_import_hint": "必填列：Email、Password、Daily Limit、From Name、SMTP Host、SMTP Port、IMAP Host、IMAP Port。每一行 Host 与 Port 都必须填写。",
        "sender_template": "下载发件箱模板",
        "sender_provider_hint": "常见 Gmail / Workspace 与 Outlook / Microsoft 365 邮箱配置参考。SMTP 方式建议使用 16 位应用专用密码；Gmail 也可使用 Gmail API OAuth 发信。",
        "smtp_host": "SMTP Host",
        "smtp_port": "SMTP Port",
        "email_security": "安全协议",
        "sender_import_check": "导入后执行 SMTP/IMAP 登录检测",
        "sender_import_uploading": "正在上传并保存发件箱...",
        "sender_import_checking": "正在导入并检测 SMTP/IMAP 登录，可能需要几分钟；请保持页面打开。",
        "sender_import_missing_file": "请上传 .xlsx 或 .csv 发件箱文件。",
        "sender_import_missing_cols": "发件箱文件必须包含 Email、Password、Daily Limit、From Name、SMTP Host、SMTP Port、IMAP Host、IMAP Port 列。",
        "sender_import_missing_required": "所有发件箱必填字段都需要填写。",
        "sender_import_done": "已导入 {count} 个发件箱。失败行：{failed}。",
        "sender_import_row_error": "第 {row} 行：{error}",
        "no_senders": "还没有配置发件箱。",
        "valid_sender_error": "请输入有效邮箱、密码、每日上限、发件人名、SMTP Host/Port 与 IMAP Host/Port。",
        "saved_sender": "已保存 {email}",
        "sender_check_passed": "SMTP 与 IMAP 登录检测通过，邮箱看起来已激活可用。",
        "sender_check_failed": "已保存 {email}，但邮箱登录检测失败：{error}",
        "smtp_check": "SMTP 检测",
        "imap_check": "IMAP 检测",
        "mailbox_check": "邮箱检测",
        "email_test_title": "Sender Score Check",
        "email_test_caption": "每个发件域名随机选择一个 active 发件箱，结合当前主题与正文模板做检测；登录 ePetrel 后会补充 DNS、认证与声誉检测。",
        "email_test_start_auth": "登录 ePetrel",
        "email_test_authorize_refresh": "授权",
        "email_test_open_auth": "打开 ePetrel 注册 / 登录",
        "email_test_check_auth": "检查授权结果",
        "email_test_auth_pending": "请点击“授权”按钮完成登录。",
        "email_test_auth_stalled": "授权仍未完成，因为 ePetrel 还没有确认授权结果。请稍后重试。",
        "email_test_authorized": "已登录 ePetrel。",
        "email_test_sender": "测试发件箱",
        "email_test_subject": "待检测模板",
        "email_test_wait": "等待结果",
        "email_test_send": "检测模板与发件域名",
        "email_test_poll": "刷新全部落箱结果",
        "email_test_no_auth": "请先完成 ePetrel 授权，再发送托管 Gmail 落箱测试。",
        "email_test_no_sender": "请先添加 active 发件箱，再运行落箱测试。",
        "email_test_sent": "已生成 {count} 个送达率检测报告。",
        "email_test_domain_limited": "{domain} 今天已经使用 {used}/3 次送达率检测。",
        "email_test_status": "请求 {request_id}：{status}",
        "email_test_result": "落箱结果：{placement}",
        "email_test_pending_help": "",
        "email_test_progress": "正在提交检测任务...",
        "email_test_domain": "域名：{domain}",
        "email_test_diagnose": "诊断 Gmail API",
        "email_test_diagnostics_title": "Gmail API 诊断",
        "email_test_diagnostics_empty": "还没有运行 Gmail API 诊断。",
        "email_test_diagnostics_running": "正在运行 Gmail API 诊断，最长可能需要 25 秒。",
        "email_test_diagnostics_ok": "Gmail API 可访问。Pending：{pending}；最近已完成：{completed}；本次扫描检查 {checked} 封，匹配 {matched} 封。",
        "email_test_diagnostics_fail": "Gmail API 诊断失败：{error}",
        "email_test_auto_poll_paused": "自动刷新已短暂停止，便于查看诊断结果。",
        "email_test_sender_status": "{sender}：{status}",
        "email_test_error": "邮件测试失败：{error}",
        "email_test_reset": "重置授权",
        "email_test_register_hint": "还没有账号？请先在 {url} 注册 ePetrel。",
        "email_test_report_title": "检测结果报告",
        "email_test_report_caption": "合并本地模板检测与 ePetrel 后端域名检测后的报告。",
        "email_test_report_empty": "完成授权检测后，这里会显示总分、分类明细、风险词与修复建议。",
        "email_test_report_pending": "ePetrel 正在后台查询 DNS、认证与声誉信息，页面会自动刷新。",
        "email_test_report_overall": "总分",
        "email_test_report_risk_words": "风险词",
        "email_test_report_no_risk_words": "未检测到风险词。",
        "email_test_report_findings": "问题明细",
        "email_test_report_no_findings": "该分类暂无明显问题。",
        "email_test_backend_error": "后端检测失败：{error}",
        "email_test_analysis_queued": "检测任务已进入队列，报告会自动刷新。",
        "email_test_analysis_status": "检测状态：{status}",
        "template_risk_preview": "风险高亮预览",
        "template_risk_preview_note": "风险词和链接会在这里随模板编辑实时高亮，报告区不再重复展示内容类检测。",
        "report_prev": "上一页",
        "report_next": "下一页",
        "report_page": "第 {page} / {pages} 页",
        "load_leads": "载入目标客户名单",
        "lead_uploader": "支持 .csv / .xlsx，必须包含 Email 列",
        "lead_file": "客户名单 CSV / Excel",
        "preview_leads": "预览名单",
        "lead_preview_title": "客户邮箱预览",
        "lead_preview_empty": "上传客户名单后，可先校验 Email 列并预览收件人。",
        "lead_preview_done": "客户名单格式可用：共 {rows} 行，{valid} 个有效邮箱。",
        "lead_preview_filename": "文件：{filename}",
        "lead_preview_page": "第 {page} / {pages} 页",
        "lead_send_status": "发送状态",
        "lead_sent": "已发送",
        "lead_unsent": "未发送",
        "lead_status": "状态",
        "lead_status_valid": "有效",
        "lead_status_invalid": "无效",
        "lead_actions": "操作",
        "delete_lead": "删除客户",
        "delete_lead_confirm": "从当前预览名单中删除这个客户吗？",
        "deleted_lead": "已删除第 {row} 行客户。",
        "clear_leads": "清空客户",
        "clear_leads_confirm": "确认清空当前客户预览名单吗？",
        "cleared_leads": "已清空当前客户预览名单。",
        "lead_preview_missing": "请先预览客户名单，再删除行。",
        "lead_file_missing": "请先上传 .csv 或 .xlsx 客户名单文件。",
        "lead_file_unsupported": "客户名单文件必须是 .csv 或 .xlsx。",
        "lead_no_valid": "客户名单中没有有效邮箱。",
        "remarketing_cooldown_label": "Remarketing Cooldown",
        "remarketing_cooldown_hint": "Contacts successfully sent within this many days are skipped. Set 0 to allow immediate remarketing.",
        "remarketing_cooldown_saved": "Saved",
        "lead_cleaning_hint": "上传前建议先使用 UseBouncer 或同类邮箱验证工具清洗名单，降低退件率，保护发件域名和邮箱信誉。",
        "custom_fields_hint": "上传文件中的任意列名都可以作为主题或正文变量，例如 {Name}、{Company}、{Company_Bio}、{Position}，也可以使用你自定义的列名。",
        "dispatch_progress_audit_hint": "For detailed sending progress, keep this page open and view Audit Logs in a new browser tab.",
        "missing_email_col": "名单缺少 Email 列。",
        "loaded_leads": "加载 {rows} 行，其中 {valid} 个邮箱格式有效。",
        "content_config": "配置多版本文案",
        "subject": "主题",
        "html_body": "正文 / Spintax 变体",
        "unsubscribe_copy": "退订说明",
        "unsubscribe_placeholder": "仅示例：不感兴趣可直接回复 no。",
        "signature": "签名",
        "signature_placeholder": "BR\n发件人姓名\n职位，公司",
        "save_unsubscribe_copy": "保存退订说明",
        "save_signature": "保存签名",
        "unsubscribe_copy_saved": "退订说明已保存，以后模板会默认使用。",
        "signature_saved": "签名已保存，以后模板会默认使用。",
        "template_library": "邮件模板库",
        "template_slot": "模板 {slot}",
        "template_empty": "空槽位",
        "template_name": "模板名称",
        "template_load": "加载",
        "template_save_current": "保存当前",
        "template_delete": "删除",
        "template_expand": "展开全部",
        "template_collapse": "只显示一个",
        "template_saved": "邮件模板 {slot} 已保存。",
        "template_loaded": "邮件模板 {slot} 已加载。",
        "template_deleted": "邮件模板 {slot} 已删除。",
        "template_missing": "这个模板槽位还是空的。",
        "template_save_confirm": "覆盖这个已保存模板吗？",
        "template_delete_confirm": "删除这个已保存模板吗？",
        "word_count_title": "冷邮件字数",
        "word_count_status": "__COUNT__ 词。建议范围：__MIN__-__MAX__ 词。",
        "generate_variants": "AI 优化送达并生成变体",
        "progress_ai_variant_title": "正在调用 LLM 优化",
        "progress_ai_variant_text": "正在生成更安全的多版本文案，通常需要几秒钟。",
        "progress_sender_check_title": "正在检测发件箱",
        "progress_sender_check_text": "保存前正在测试 SMTP 与 IMAP 登录状态。",
        "progress_gmail_oauth_title": "正在准备 Gmail 授权",
        "progress_gmail_oauth_text": "即将打开 Google 授权，授权完成后会保存发件箱。",
        "progress_lead_preview_title": "正在预览客户名单",
        "progress_lead_preview_text": "正在读取上传文件并校验收件邮箱。",
        "progress_inbox_sync_title": "正在同步收件箱",
        "progress_inbox_sync_text": "正在从已保存发件箱拉取近期回信。",
        "progress_seed_sync_title": "正在同步 Seed 落箱",
        "progress_seed_sync_text": "正在扫描 seed 收件箱和垃圾箱中的落箱事件。",
        "progress_llm_save_title": "正在保存 LLM 设置",
        "progress_llm_save_text": "正在加密保存 API key 并更新本地模型配置。",
        "variant_help": "可使用 {Name}、{Company}、{Company_Bio}、{Position} 等变量。你可以自己填写 {版本A|版本B} 变体，也可以使用 AI 优化，让系统用更利于送达的多版本正文替换当前内容。",
        "variant_action_hint": "将正文改成低风险的一对一语气，弱化营销词，保留变量和链接，并生成安全的 Spintax 变体。",
        "variant_format_error": "文案变体格式存在未闭合大括号或空的 Spintax 选项。",
        "template_variable_missing_cols": "已阻止发送：模板变量在客户名单中缺少对应列：{columns}。请添加这些列，或从模板中删除这些变量。",
        "template_variable_empty_values": "已阻止发送：以下客户行的模板变量为空：{details}。请补全内容或删除这些行后再发送。",
        "variant_generated": "AI 已优化文案，并替换为更利于送达的多版本内容。",
        "variant_generate_failed": "AI 未返回有效的优化 Spintax 版本。请重试，或先简化正文并保留变量。",
        "reputation_ps_hint": "预览会合并正文、退订说明和签名；退订与签名均可编辑，并同样参与风险词和链接提示。",
        "queue_control": "控流与队列控制",
        "delay_min": "最小间隔（s）",
        "delay_max": "最大间隔（s）",
        "use_ai": "AI 实时破冰句",
        "variant": "版本标记",
        "mix_seed": "混入 seed 测试邮箱",
        "seed_interval": "Seed 间隔",
        "start_queue": "启动自主轮询发信",
        "available_senders": "当前可用发件箱：{count} 个",
        "batch_done": "恭喜，当前发信队列已成功完成。",
        "dispatch_working_title": "发信队列正在执行",
        "dispatch_working_body": "系统正在发送邮件，请保持页面打开；客户名单的已发送状态会自动更新。",
        "dispatch_stop": "停止",
        "dispatch_stopping": "正在停止...",
        "dispatch_stop_requested": "已请求停止，系统会在当前发送或等待结束后停止队列。",
        "dispatch_stopped": "发信队列已由用户停止。",
        "security_title": "发件安全监控",
        "security_caption": "基于本地发送日志、IMAP 退信解析、退订识别和 seed 落箱采样。",
        "seed_pool": "Seed 测试邮箱池",
        "no_seeds": "还没有配置 seed 测试邮箱。",
        "seed_email": "Seed 邮箱",
        "seed_password": "IMAP 密码 / App Password",
        "provider": "服务商",
        "imap_host": "IMAP Host",
        "imap_port": "IMAP Port",
        "inbox_folder": "Inbox 文件夹",
        "spam_folder": "Spam/Junk 文件夹",
        "status": "状态",
        "save_seed": "保存 Seed 邮箱",
        "saved_seed": "已保存 seed 邮箱 {email}",
        "valid_seed_error": "请输入有效 seed 邮箱、密码和 IMAP Host。",
        "days_window": "统计窗口 / 天",
        "seed_limit": "每个 seed 文件夹扫描邮件数",
        "sync_seed": "立即同步 Seed 落箱",
        "no_active_seed": "还没有 active seed 邮箱。",
        "seed_sync_success": "{seed}: 匹配 {matched} 封，missing 新增 {missing} 条",
        "clear_seeds": "清空 Seed",
        "clear_seeds_confirm": "确认清空这个本地客户端中的全部 seed 测试邮箱吗？",
        "cleared_seeds": "已清空 {count} 个 seed 测试邮箱。",
        "clear_security_outbound": "清空发送摘要",
        "clear_security_outbound_confirm": "确认清空发送摘要数据吗？发件箱今日计数会按剩余审计记录重新计算。",
        "cleared_security_outbound": "已清空 {count} 条发送记录。",
        "clear_security_events": "清空事件",
        "clear_security_events_confirm": "确认清空全部安全事件历史吗？",
        "cleared_security_events": "已清空 {count} 条安全事件。",
        "metric_sent": "成功发送",
        "metric_failed": "SMTP 失败",
        "metric_bounce": "总退信率",
        "metric_hard": "Hard Bounce",
        "metric_unsub": "退订率",
        "metric_spam": "Seed Spam",
        "no_alerts": "当前统计窗口未触发安全阈值。",
        "sender_domain_summary": "按发件箱 / 域名汇总",
        "event_details": "事件明细",
        "sender_health": "发件箱健康",
        "audit_title": "历史发信全留底审查中心",
        "audit_caption": "审查原始正文、状态、失败原因与 Message-ID。",
        "audit_actions": "操作",
        "audit_error": "原因",
        "audit_filter_status": "状态",
        "audit_filter_all": "全部",
        "audit_filter_success": "成功",
        "audit_filter_failed": "失败",
        "audit_filter_skipped": "跳过",
        "audit_filter_unsent": "失败 + 跳过",
        "audit_filter_sender": "发件箱",
        "audit_filter_receiver": "收件人",
        "audit_filter_domain": "域名",
        "audit_filter_error": "原因包含",
        "audit_apply_filters": "筛选",
        "audit_reset_filters": "重置",
        "audit_export_unsent": "导出未成功收件人",
        "audit_export_empty": "当前筛选下没有 failed / skipped 收件邮箱可导出。",
        "audit_delete": "删除记录",
        "audit_delete_confirm": "确认删除这条审计记录吗？发件箱今日计数会按剩余成功审计记录重新计算。",
        "audit_deleted": "已删除审计记录 #{id}，并已按审计记录重新计算发件箱计数。",
        "audit_delete_missing": "未找到该审计记录。",
        "audit_clear": "一键清除审计记录",
        "audit_clear_confirm": "确认清空全部发信审计记录吗？发件箱今日计数会按剩余审计记录重新计算。",
        "audit_cleared": "已清除 {count} 条审计记录，并已按审计记录重新计算发件箱计数。",
        "raw_trace": "邮件原文渲染追溯",
        "select_email_id": "输入想要审查的邮件 ID",
        "fetch_html": "拉取原始 HTML 留底",
        "not_found": "未找到该 ID 对应的邮件记录。",
        "inbox_title": "统一共享收件箱",
        "inbox_caption": "聚合 Mail 发件箱回信，并自动识别退订、拒绝、高意向邮件。",
        "fetch_limit": "每个邮箱拉取最近邮件数",
        "sync_inbox": "立即同步收件箱",
        "inbox_sync_success": "{sender}: 新增 {stored} 封",
        "empty_inbox": "目前还没有收到客户回信。",
        "llm_title": "LLM Provider 设置",
        "llm_caption": "安全保存 API key，选择 OpenAI / DeepSeek 等兼容接口，或 Anthropic Claude，并调整默认系统提示词。",
        "active_provider": "通讯协议",
        "api_key": "API Key",
        "base_url": "Base URL",
        "model": "模型",
        "system_prompt": "系统提示词",
        "save_llm": "保存 LLM 设置",
        "llm_saved": "LLM 设置已保存。",
        "llm_missing_key": "当前 provider 尚未保存 API key。AI 功能会使用兜底文案，直到保存 key。",
        "current_llm": "当前 LLM 配置",
        "warm_current_llm": "Warm 专用 LLM 配置",
        "cold_llm_caption": "用于冷邮件文案、个性化破冰句和送达率友好的变体生成，建议使用效果最好的模型。",
        "warm_llm_caption": "用于生成短小自然的 warm 邮箱对话，建议使用成本最低且稳定的模型。",
        "save_warm_llm": "保存 Warm LLM 设置",
        "toolkit": "Provider Toolkit",
        "openai_toolkit": "OpenAI / OpenAI 兼容协议使用 Chat Completions。OpenAI、DeepSeek 或其他兼容 OpenAI 接口的服务都走这里：填入对应服务商的 API key、Base URL 和准确模型名即可。",
        "anthropic_toolkit": "Anthropic Claude 使用官方 Messages API：system 是顶层字段，user content 放入 messages，并且必须提供 max_tokens。",
        "security_note": "安全措施：API key 使用密码输入框，不在表格中明文渲染，保存后脱敏显示，并在安装 cryptography 后本地加密存储。",
        "system_prompt_help": "系统提示词会影响 AI 破冰句和文案变体生成。修改时请特别保留“不要改坏变量、只输出合法 Spintax”的约束，否则可能破坏个性化字段或发送格式。",
    },
}


def t(lang, key, **kwargs):
    value = TEXT.get(lang, TEXT["en"]).get(key, TEXT["en"].get(key, key))
    return value.format(**kwargs) if kwargs else value


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
app_logger = configure_file_logger("epetrel.app", LOG_DIR / "app_errors.log", level=logging.INFO)
dispatch_logger = configure_file_logger("epetrel.dispatch", LOG_DIR / "dispatch_errors.log", level=logging.INFO)
email_test_logger = configure_file_logger("epetrel.email_test", LOG_DIR / "email_test.log", level=logging.INFO)
gmail_oauth_logger = configure_file_logger("epetrel.gmail_oauth", LOG_DIR / "gmail_oauth.log", level=logging.INFO)
warm_worker_logger = configure_file_logger("epetrel.warm_worker", LOG_DIR / "warm_worker.log", level=logging.INFO)

EMAIL_TEST_CACHE_TTL_SECONDS = 60 * 60
EMAIL_TEST_REPORT_CACHE = {}
EMAIL_TEST_LOCAL_REPORT_CACHE = {}
EMAIL_TEST_AUTH_CACHE = {}
GMAIL_OAUTH_PENDING = {}
LEAD_PREVIEW_CACHE = {}
LEAD_PREVIEW_PAGE_SIZE = 8
LEAD_PREVIEW_TTL_SECONDS = 60 * 60
LEAD_PREVIEW_DATA_SETTING_KEY = "dispatch_lead_preview_json"
LEAD_PREVIEW_FILENAME_SETTING_KEY = "dispatch_lead_preview_filename"
WARM_AUTH_SETTING_KEY = "warm_auth_json"
AUDIT_PAGE_SIZE = 30
AUDIT_EXPORT_STATUSES = {"failed", "skipped"}
CRM_PAGE_SIZE = 50
CRM_OBSIDIAN_PATH_SETTING_KEY = "crm_obsidian_export_path"
CRM_MAX_REMARKETING_SETTING_KEY = "crm_max_remarketing_attempts"
CRM_DEFAULT_OBSIDIAN_DIR = BASE_DIR / "exports" / "obsidian" / "crm"
CRM_STANDARD_FIELD_ALIASES = {
    "name": ["name", "full name", "contact name", "姓名", "名字"],
    "company": ["company", "company name", "account", "公司", "客户公司"],
    "position": ["position", "title", "job title", "role", "职位", "岗位"],
    "company_bio": ["company_bio", "company bio", "company introduction", "intro", "介绍", "公司介绍", "客户介绍"],
    "website": ["website", "site", "url", "company website", "官网"],
    "phone": ["phone", "mobile", "telephone", "电话", "手机号"],
    "whatsapp": ["whatsapp", "whats app", "wa", "whatsapp url"],
    "instagram": ["instagram", "ins", "instagram url", "ig"],
    "linkedin": ["linkedin", "linkedin url", "linkedin profile"],
    "country": ["country", "region", "location", "国家", "地区"],
    "tags": ["tags", "tag", "标签"],
    "notes": ["notes", "note", "remark", "remarks", "备注"],
    "campaign": ["campaign", "campaign name", "活动", "营销活动"],
    "source": ["source", "lead source", "来源"],
}


@app.exception_handler(Exception)
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


def _email_test_cache_set(store, key, value, ttl_seconds=EMAIL_TEST_CACHE_TTL_SECONDS):
    if key:
        store[str(key)] = {"expires_at": time.time() + max(60, int(ttl_seconds)), "value": value}


def _email_test_cache_get(store, key, default=None):
    if not key:
        return default
    record = store.get(str(key))
    if not record:
        return default
    if float(record.get("expires_at") or 0) < time.time():
        store.pop(str(key), None)
        return default
    return record.get("value", default)


def _email_test_cache_delete(store, key):
    if key:
        store.pop(str(key), None)


def _email_test_auth_is_authorized(auth_data):
    if not isinstance(auth_data, dict):
        return False
    status = str(auth_data.get("status") or "").lower()
    return bool(auth_data.get("access_token")) or status == "authorized"


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
    if not _email_test_auth_is_authorized(auth_data):
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
    if not _email_test_auth_is_authorized(data) or warm_auth_is_expired(data):
        clear_persisted_warm_auth()
        return {}
    return data


def _session_epetrel_auth(request, key):
    auth_data = request.session.get(key) or {}
    if key == "warm_auth" and (not _email_test_auth_is_authorized(auth_data) or warm_auth_is_expired(auth_data)):
        persisted_auth = load_persisted_warm_auth()
        if _email_test_auth_is_authorized(persisted_auth):
            auth_data = persisted_auth
            request.session[key] = auth_data
    if _email_test_auth_is_authorized(auth_data):
        if key == "warm_auth":
            set_warm_worker_auth(auth_data)
        return auth_data
    return {}


def _store_epetrel_auth(request, key, auth_data, device_code=""):
    request.session[key] = auth_data
    if device_code:
        _email_test_cache_set(EMAIL_TEST_AUTH_CACHE, device_code, auth_data, ttl_seconds=10 * 60)


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
    request.session["email_test_auth"] = {}
    request.session["email_test_auth_request"] = {}
    request.session["email_test_auth_started_at"] = 0


def _email_test_auth(request):
    return _session_epetrel_auth(request, "warm_auth")


def _email_test_auth_request(request):
    return request.session.get("warm_auth_request") or {}


def _email_test_sync_auth_from_bff(request):
    auth_data = _email_test_auth(request)
    if _email_test_auth_is_authorized(auth_data):
        return auth_data

    auth_request = _email_test_auth_request(request)
    device_code = auth_request.get("device_code", "")
    if not device_code:
        return auth_data

    cached_auth = _email_test_cache_get(EMAIL_TEST_AUTH_CACHE, device_code)
    if _email_test_auth_is_authorized(cached_auth):
        _warm_store_auth(request, cached_auth, device_code)
        return cached_auth

    try:
        polled = poll_warm_auth(device_code)
    except WarmApiError as exc:
        email_test_logger.warning("email test auth sync failed device_code=%s error=%s", device_code[:16], exc)
        return auth_data

    if _email_test_auth_is_authorized(polled):
        _warm_store_auth(request, polled, device_code)
        email_test_logger.info("email test auth synced during dispatch render device_code=%s", device_code[:16])
        return polled

    return auth_data

SENDER_TEMPLATE_PATH = BASE_DIR / "static" / "templates" / "senderemaillist.xlsx"
REQUIRED_SENDER_FIELDS = [
    "email",
    "password",
    "daily_limit",
    "from_name",
    "smtp_host",
    "smtp_port",
    "imap_host",
    "imap_port",
]
LEGACY_DEFAULT_UNSUBSCRIBE_COPY = "Not interested? Just reply 'no'."
DEFAULT_UNSUBSCRIBE_COPY = ""
DEFAULT_SIGNATURE = ""
UNSUBSCRIBE_COPY_SETTING_KEY = "dispatch_unsubscribe_copy"
SIGNATURE_SETTING_KEY = "dispatch_signature"
REMARKETING_COOLDOWN_SETTING_KEY = "dispatch_remarketing_cooldown_days"
EMAIL_TEMPLATE_SLOT_COUNT = 5
DISPATCH_STOP_REQUESTS = set()
GMAIL_ACCOUNT_TYPES = {"consumer_gmail", "workspace_gmail"}
GMAIL_ACCOUNT_TYPE_DAILY_LIMITS = {"consumer_gmail": 20, "workspace_gmail": 40}
MAIL_PROVIDER_ROWS = [
    {
        "provider": "Gmail / Workspace",
        "purpose": "SMTP sending",
        "host": "smtp.gmail.com",
        "port": "465 or 587",
        "security": "SSL on 465; STARTTLS/TLS on 587",
    },
    {
        "provider": "Gmail / Workspace",
        "purpose": "IMAP receiving",
        "host": "imap.gmail.com",
        "port": "993",
        "security": "SSL/TLS",
    },
    {
        "provider": "Outlook / Microsoft 365",
        "purpose": "SMTP sending",
        "host": "smtp.office365.com or smtp-mail.outlook.com",
        "port": "587",
        "security": "STARTTLS",
    },
    {
        "provider": "Outlook / Microsoft 365",
        "purpose": "IMAP receiving",
        "host": "outlook.office365.com or imap-mail.outlook.com",
        "port": "993",
        "security": "SSL/TLS",
    },
]


def normalize_gmail_account_type(value, email=""):
    value = (value or "").strip().lower()
    if value in GMAIL_ACCOUNT_TYPES:
        return value
    domain = get_domain(normalize_email(email))
    return "consumer_gmail" if domain in {"gmail.com", "googlemail.com"} else "workspace_gmail"


def gmail_account_default_daily_limit(account_type):
    return GMAIL_ACCOUNT_TYPE_DAILY_LIMITS.get(account_type, DEFAULT_DAILY_LIMIT)


def provider_label(provider):
    return "Anthropic Claude" if provider == "anthropic" else "OpenAI / Compatible"


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


EMAIL_TEST_SECTION = "/dispatch#email-test-section"


def normalize_unsubscribe_copy(value):
    value = (value or "").strip()
    if value == LEGACY_DEFAULT_UNSUBSCRIBE_COPY:
        return ""
    return value


def normalize_remarketing_cooldown_days(value):
    try:
        return max(0, min(int(value), 3650))
    except (TypeError, ValueError):
        return max(0, int(REMARKETING_COOLDOWN_DAYS or 0))


def get_remarketing_cooldown_days():
    saved = get_app_setting(REMARKETING_COOLDOWN_SETTING_KEY, str(REMARKETING_COOLDOWN_DAYS))
    return normalize_remarketing_cooldown_days(saved)


def dispatch_client_id(request):
    client_id = request.session.get("dispatch_client_id")
    if not client_id:
        client_id = f"dispatch_{uuid.uuid4()}"
        request.session["dispatch_client_id"] = client_id
    return client_id


def dispatch_stop_requested(request):
    return request.session.get("dispatch_client_id", "") in DISPATCH_STOP_REQUESTS


async def dispatch_sleep(request, seconds):
    remaining = max(0, int(seconds or 0))
    while remaining > 0 and not dispatch_stop_requested(request):
        interval = min(1, remaining)
        await asyncio.sleep(interval)
        remaining -= interval


def get_lang(request):
    request.session["language"] = "en"
    return "en"


def page_context(request, page, title_key, caption_key, **extra):
    lang = get_lang(request)
    dispatch_client_id(request)
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


def clean_cell(value, default=""):
    if pd.isna(value):
        return default
    return str(value).strip()


def clamp_crm_max_attempts(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = CRM_DEFAULT_REMARKETING_MAX
    return max(0, min(CRM_HARD_REMARKETING_MAX, value))


def get_crm_max_remarketing_attempts():
    return clamp_crm_max_attempts(get_app_setting(CRM_MAX_REMARKETING_SETTING_KEY, str(CRM_DEFAULT_REMARKETING_MAX)))


def set_crm_max_remarketing_attempts(value):
    attempts = clamp_crm_max_attempts(value)
    upsert_app_setting(CRM_MAX_REMARKETING_SETTING_KEY, str(attempts))
    return attempts


def split_tags(value):
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[,;，、]+", str(value or ""))
    tags = []
    seen = set()
    for item in raw:
        tag = " ".join(str(item or "").strip().split())
        key = tag.lower()
        if tag and key not in seen:
            tags.append(tag[:80])
            seen.add(key)
    return tags


def _record_column_lookup(record):
    return {str(key).strip().lower(): key for key in (record or {}).keys()}


def lead_field_value(record, field):
    lookup = _record_column_lookup(record)
    for alias in CRM_STANDARD_FIELD_ALIASES.get(field, []):
        column = lookup.get(alias.lower())
        if column is not None:
            return clean_cell(record.get(column))
    return ""


def crm_payload_from_lead_record(record):
    lookup = _record_column_lookup(record)
    known_columns = {"email"}
    for aliases in CRM_STANDARD_FIELD_ALIASES.values():
        for alias in aliases:
            column = lookup.get(alias.lower())
            if column is not None:
                known_columns.add(str(column).strip().lower())
    custom_fields = {}
    for key, value in (record or {}).items():
        key_text = str(key or "").strip()
        if not key_text or key_text.lower() in known_columns:
            continue
        text = clean_cell(value)
        if text:
            custom_fields[key_text] = text
    return {
        "email": normalize_email(record.get("Email", "")),
        "name": lead_field_value(record, "name"),
        "company": lead_field_value(record, "company"),
        "position": lead_field_value(record, "position"),
        "company_bio": lead_field_value(record, "company_bio"),
        "website": lead_field_value(record, "website"),
        "phone": lead_field_value(record, "phone"),
        "country": lead_field_value(record, "country"),
        "source": lead_field_value(record, "source"),
        "campaign": lead_field_value(record, "campaign"),
        "notes": lead_field_value(record, "notes"),
        "whatsapp": lead_field_value(record, "whatsapp"),
        "instagram": lead_field_value(record, "instagram"),
        "linkedin": lead_field_value(record, "linkedin"),
        "tags": split_tags(lead_field_value(record, "tags")),
        "custom_fields": custom_fields,
    }


def upsert_crm_contacts_from_records(records, max_attempts=None):
    saved = 0
    for record in records or []:
        payload = crm_payload_from_lead_record(record)
        if not payload["email"]:
            continue
        upsert_crm_contact(
            payload["email"],
            name=payload["name"],
            company=payload["company"],
            position=payload["position"],
            company_bio=payload["company_bio"],
            website=payload["website"],
            phone=payload["phone"],
            country=payload["country"],
            source=payload["source"],
            campaign=payload["campaign"],
            notes=payload["notes"],
            whatsapp=payload["whatsapp"],
            instagram=payload["instagram"],
            linkedin=payload["linkedin"],
            custom_fields=payload["custom_fields"],
            tags=payload["tags"],
            max_remarketing_attempts=max_attempts,
        )
        saved += 1
    return saved


def crm_next_followup_at(days):
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return None
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def crm_contact_record_for_template(contact):
    custom = contact.get("custom_fields") if isinstance(contact.get("custom_fields"), dict) else {}
    record = {key: value for key, value in custom.items()}
    record.update(
        {
            "Email": contact.get("email", ""),
            "Name": contact.get("name", ""),
            "Company": contact.get("company", ""),
            "Position": contact.get("position", ""),
            "Company_Bio": contact.get("company_bio", ""),
            "Website": contact.get("website", ""),
            "Phone": contact.get("phone", ""),
            "WhatsApp": contact.get("whatsapp", ""),
            "Instagram": contact.get("instagram", ""),
            "LinkedIn": contact.get("linkedin", ""),
            "Country": contact.get("country", ""),
            "Campaign": contact.get("campaign", ""),
            "Source": contact.get("source", ""),
        }
    )
    return record


def crm_status_options():
    return [
        "pending",
        "replied_pending_review",
        "interested",
        "follow_up_later",
        "not_interested",
        "bounced",
        "abandoned",
    ]


def obsidian_safe_name(value, fallback="contact"):
    value = re.sub(r"[\\/:*?\"<>|#^\[\]]+", "-", str(value or "").strip())
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:120]


def yaml_value(value):
    text = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def obsidian_wikilink(prefix, value):
    value = str(value or "").strip()
    return f"[[{prefix} {value}]]" if value else ""


def export_crm_obsidian_notes(target_dir=""):
    target = Path(target_dir or get_app_setting(CRM_OBSIDIAN_PATH_SETTING_KEY, "") or CRM_DEFAULT_OBSIDIAN_DIR)
    if not target.is_absolute():
        target = BASE_DIR / target
    target.mkdir(parents=True, exist_ok=True)
    contacts = list_crm_contacts_for_export({})
    written = 0
    for contact in contacts:
        email = contact.get("email", "")
        if not email:
            continue
        filename_base = obsidian_safe_name(contact.get("company") or contact.get("name") or email, fallback=email)
        note_path = target / f"{filename_base} - {obsidian_safe_name(email)}.md"
        tags = contact.get("tags") or []
        channels = [name for name in ["whatsapp", "instagram", "linkedin"] if contact.get(name)]
        links = [
            obsidian_wikilink("Company", contact.get("company")),
            obsidian_wikilink("campaign", contact.get("campaign")),
            obsidian_wikilink("status", contact.get("status")),
            *[obsidian_wikilink("tag", tag) for tag in tags],
            *[obsidian_wikilink("channel", channel) for channel in channels],
        ]
        links = [link for link in links if link]
        frontmatter = [
            "---",
            f"email: {yaml_value(email)}",
            f"company: {yaml_value(contact.get('company'))}",
            f"status: {yaml_value(contact.get('status'))}",
            "tags: [" + ", ".join(yaml_value(tag) for tag in tags) + "]",
            "channels: [" + ", ".join(yaml_value(channel) for channel in channels) + "]",
            f"last_reply_at: {yaml_value(contact.get('last_reply_at'))}",
            f"next_followup_at: {yaml_value(contact.get('next_followup_at'))}",
            "---",
        ]
        custom_fields = contact.get("custom_fields") or {}
        body = [
            f"# {contact.get('name') or email}",
            "",
            f"- Email: {email}",
            f"- Company: {contact.get('company') or ''}",
            f"- Position: {contact.get('position') or ''}",
            f"- Website: {contact.get('website') or ''}",
            f"- Phone: {contact.get('phone') or ''}",
            f"- WhatsApp: {contact.get('whatsapp') or ''}",
            f"- Instagram: {contact.get('instagram') or ''}",
            f"- LinkedIn: {contact.get('linkedin') or ''}",
            f"- Source: {contact.get('source') or ''}",
            f"- Campaign: {contact.get('campaign') or ''}",
            "",
            "## Graph Links",
            " ".join(links) if links else "",
            "",
            "## Notes",
            contact.get("notes") or "",
            "",
            "## Company Bio",
            contact.get("company_bio") or "",
            "",
            "## Custom Fields",
        ]
        for key, value in sorted(custom_fields.items()):
            body.append(f"- {key}: {value}")
        note_path.write_text("\n".join(frontmatter + body).strip() + "\n", encoding="utf-8")
        written += 1
    return {"count": written, "path": str(target)}


DEFAULT_SUBJECT_TEMPLATE = "Quick idea for {Company}"
LEGACY_DEFAULT_SUBJECT_TEMPLATE = "{Hi|Hello} {Name}, quick idea for {Company}"


def normalize_subject_template(subject):
    value = subject or DEFAULT_SUBJECT_TEMPLATE
    if re.sub(r"\s+", " ", value).strip().lower() == re.sub(r"\s+", " ", LEGACY_DEFAULT_SUBJECT_TEMPLATE).strip().lower():
        return DEFAULT_SUBJECT_TEMPLATE

    def remove_hello_option(match):
        options = [item.strip() for item in match.group(1).split("|")]
        filtered = [item for item in options if item.lower() != "hello"]
        if not filtered:
            return ""
        if len(filtered) == 1:
            return filtered[0]
        return "{" + "|".join(filtered) + "}"

    value = re.sub(r"\{([^{}]*\|[^{}]*)\}", remove_hello_option, value)
    return re.sub(r"\bhello\b", "Hi", value, flags=re.IGNORECASE)


def strip_unresolved_template_markers(text):
    cleaned = re.sub(r"\{([^{}]+)\}", lambda match: match.group(1).strip(), text or "")
    return cleaned.replace("{", "").replace("}", "")


SYSTEM_TEMPLATE_VARIABLES = {"AI_Icebreaker"}


def extract_template_variables(*texts):
    variables = []
    seen = set()
    for text in texts:
        for match in re.finditer(r"\{([^{}]+)\}", text or ""):
            name = match.group(1).strip()
            if not name or "|" in name or name in SYSTEM_TEMPLATE_VARIABLES:
                continue
            if name not in seen:
                seen.add(name)
                variables.append(name)
    return variables


def template_variable_errors(df, *texts):
    variables = extract_template_variables(*texts)
    if not variables:
        return [], []

    columns = {str(column).strip(): column for column in df.columns}
    missing = [name for name in variables if name not in columns]
    checked_variables = [name for name in variables if name in columns]
    empty_rows = []
    if checked_variables:
        for index, row in df.iterrows():
            if not normalize_email(row.get("Email", "")):
                continue
            empty_names = [
                name
                for name in checked_variables
                if not clean_cell(row.get(columns[name], ""))
            ]
            if empty_names:
                empty_rows.append({"row": int(index) + 2, "variables": empty_names})
    return missing, empty_rows


def render_template_text(template, record, icebreaker):
    record_columns = {str(key).strip(): key for key in record.keys()}

    def replace_variable(match):
        name = match.group(1).strip()
        if name == "AI_Icebreaker":
            return icebreaker
        if name in record_columns:
            return clean_cell(record.get(record_columns[name]))
        return match.group(0)

    rendered = re.sub(r"\{\s*([^{}|]+?)\s*\}", replace_variable, template or "")
    return strip_unresolved_template_markers(rendered)


def render_variant_template(template, record, icebreaker, seed=None):
    variant_text = parse_spintax(template or "", seed=seed)
    return render_template_text(variant_text, record, icebreaker)


def normalize_lead_dataframe(df):
    df = df.copy()
    df.columns = [str(column).strip() for column in df.columns]
    if "Email" not in df.columns:
        for column in df.columns:
            if str(column).strip().lower() in {"email", "e-mail", "mail", "邮箱", "客户邮箱"}:
                df = df.rename(columns={column: "Email"})
                break
    return df


async def load_lead_dataframe(uploaded_file, allow_sample=True):
    if uploaded_file is None or not uploaded_file.filename:
        if not allow_sample:
            return None
        return pd.DataFrame(
            {
                "Email": ["test_lead@gmail.com"],
                "Name": ["Leo"],
                "Company": ["Zhenhezhijing"],
                "Company_Bio": ["AI startup company"],
                "Position": ["CEO"],
                "LinkedIn": ["https://www.linkedin.com/in/example"],
                "Instagram": ["https://www.instagram.com/example"],
                "WhatsApp": ["+61400000000"],
                "Tags": ["sample, SaaS"],
                "Notes": ["Sample CRM lead"],
            }
        )
    content = await uploaded_file.read()
    filename = uploaded_file.filename.lower()
    if filename.endswith(".csv"):
        return normalize_lead_dataframe(pd.read_csv(BytesIO(content)))
    if filename.endswith(".xlsx"):
        return normalize_lead_dataframe(pd.read_excel(BytesIO(content)))
    return None


async def load_uploaded_dataframe(uploaded_file):
    if uploaded_file is None or not uploaded_file.filename:
        return None
    content = await uploaded_file.read()
    filename = uploaded_file.filename.lower()
    if filename.endswith(".csv"):
        return pd.read_csv(BytesIO(content))
    if filename.endswith(".xlsx"):
        return pd.read_excel(BytesIO(content))
    return None


def count_valid_leads(df):
    if "Email" not in df.columns:
        return 0
    return sum(1 for value in df["Email"] if normalize_email(value))


def records_from_df(df, limit=None):
    clean = df.fillna("")
    if limit:
        clean = clean.head(limit)
    return clean.to_dict(orient="records")


def lead_preview_from_df(df, lang, filename="", page=1):
    empty = {
        "filename": filename,
        "rows": [],
        "columns": [],
        "total": 0,
        "valid": 0,
        "invalid": 0,
        "page": 1,
        "pages": 1,
        "has_prev": False,
        "has_next": False,
        "prev_url": "",
        "next_url": "",
        "page_label": t(lang, "lead_preview_page", page=1, pages=1),
    }
    if df is None or "Email" not in df.columns:
        return empty

    clean = df.fillna("")
    total = len(clean)
    valid = count_valid_leads(clean)
    pages = max(1, (total + LEAD_PREVIEW_PAGE_SIZE - 1) // LEAD_PREVIEW_PAGE_SIZE)
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * LEAD_PREVIEW_PAGE_SIZE
    display_columns = [column for column in ["Email", "Name", "Company", "Position"] if column in clean.columns]
    for column in clean.columns:
        if column not in display_columns and len(display_columns) < 5:
            display_columns.append(column)
    sent_receivers = list_successful_receivers(
        normalize_email(value) for value in clean["Email"]
    )

    rows = []
    for offset, row in enumerate(clean.iloc[start:start + LEAD_PREVIEW_PAGE_SIZE].to_dict(orient="records")):
        email = normalize_email(row.get("Email", ""))
        rows.append(
            {
                "number": start + offset + 1,
                "email": email,
                "is_valid": bool(email),
                "is_sent": bool(email and email in sent_receivers),
                "cells": {column: clean_cell(row.get(column)) for column in display_columns},
            }
        )

    return {
        "filename": filename,
        "rows": rows,
        "columns": display_columns,
        "total": total,
        "valid": valid,
        "invalid": max(0, total - valid),
        "page": page,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev_url": f"/dispatch?lead_page={page - 1}#lead-section" if page > 1 else "",
        "next_url": f"/dispatch?lead_page={page + 1}#lead-section" if page < pages else "",
        "page_label": t(lang, "lead_preview_page", page=page, pages=pages),
    }


def serialize_lead_dataframe(df):
    if df is None:
        return ""
    return df.fillna("").to_json(orient="split", force_ascii=False)


def deserialize_lead_dataframe(payload):
    payload = (payload or "").strip()
    if not payload:
        return None
    try:
        return normalize_lead_dataframe(pd.read_json(BytesIO(payload.encode("utf-8")), orient="split", dtype=False))
    except Exception as exc:
        app_logger.exception("lead preview restore failed error=%s", redact_sensitive(str(exc)))
        return None


def persist_lead_preview(request, df, filename=""):
    set_cached_lead_dataframe(request, df)
    if filename:
        request.session["lead_preview_filename"] = filename
        upsert_app_setting(LEAD_PREVIEW_FILENAME_SETTING_KEY, filename)
    upsert_app_setting(LEAD_PREVIEW_DATA_SETTING_KEY, serialize_lead_dataframe(df))


def get_cached_lead_dataframe(request):
    preview_id = request.session.get("lead_preview_id", "")
    df = _email_test_cache_get(LEAD_PREVIEW_CACHE, preview_id)
    if df is None:
        df = deserialize_lead_dataframe(get_app_setting(LEAD_PREVIEW_DATA_SETTING_KEY, ""))
        if df is not None:
            set_cached_lead_dataframe(request, df)
            filename = get_app_setting(LEAD_PREVIEW_FILENAME_SETTING_KEY, "")
            if filename and not request.session.get("lead_preview_filename"):
                request.session["lead_preview_filename"] = filename
    return df


def clear_lead_preview(request):
    _email_test_cache_delete(LEAD_PREVIEW_CACHE, request.session.get("lead_preview_id", ""))
    request.session.pop("lead_preview_id", None)
    request.session.pop("lead_preview_filename", None)
    upsert_app_setting(LEAD_PREVIEW_DATA_SETTING_KEY, "")
    upsert_app_setting(LEAD_PREVIEW_FILENAME_SETTING_KEY, "")


def set_cached_lead_dataframe(request, df):
    preview_id = request.session.get("lead_preview_id", "")
    if not preview_id:
        preview_id = f"lead_{uuid.uuid4()}"
        request.session["lead_preview_id"] = preview_id
    _email_test_cache_set(LEAD_PREVIEW_CACHE, preview_id, df, ttl_seconds=LEAD_PREVIEW_TTL_SECONDS)


def generate_sender_template_bytes():
    df = pd.DataFrame(
        [
            {
                "Email": "sender@gmail.com",
                "Password": "app-password",
                "Daily Limit": 40,
                "From Name": "Your Name",
                "SMTP Host": "smtp.gmail.com",
                "SMTP Port": 587,
                "IMAP Host": "imap.gmail.com",
                "IMAP Port": 993,
            },
            {
                "Email": "sender@outlook.com",
                "Password": "app-password",
                "Daily Limit": 40,
                "From Name": "Your Name",
                "SMTP Host": "smtp-mail.outlook.com",
                "SMTP Port": 587,
                "IMAP Host": "outlook.office365.com",
                "IMAP Port": 993,
            },
        ]
    )
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Senders")
    output.seek(0)
    return output


def _protect_non_spintax_placeholders(text):
    protected = text or ""
    tokens = {}

    def remember(match):
        token = f"__EPETREL_SAFE_TOKEN_{len(tokens)}__"
        tokens[token] = match.group(0)
        return token

    protected = re.sub(r"https?://[^\s<>'\"]+|www\.[^\s<>'\"]+", remember, protected)
    protected = re.sub(r"\{\{[^{}]+\}\}", remember, protected)
    protected = re.sub(r"\[[^\[\]\r\n]{1,100}\]", remember, protected)
    return protected


def validate_spintax_format(text):
    text = _protect_non_spintax_placeholders(text)
    stack = []
    for char in text or "":
        if char == "{":
            if stack:
                return False
            stack.append(char)
        elif char == "}":
            if not stack:
                return False
            stack.pop()
    if stack:
        return False

    for match in re.finditer(r"\{([^{}]*\|[^{}]*)\}", text or ""):
        options = [item.strip() for item in match.group(1).split("|")]
        if any(not item for item in options):
            return False
    return True


def contains_spintax_variants(text):
    return any("|" in match.group(1) for match in re.finditer(r"\{([^{}]+)\}", text or ""))


def normalize_copy_for_compare(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def template_has_html(text):
    return bool(re.search(r"<\s*(p|div|br|table|ul|ol|html|body)\b", text or "", re.IGNORECASE))


def text_section_to_html(section):
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", section or "") if part.strip()]
    return "\n".join(f"<p>{escape(part).replace(chr(10), '<br>')}</p>" for part in paragraphs)


def compose_email_template(body, unsubscribe_copy=None, signature=None):
    sections = [
        (body or "").strip(),
        (unsubscribe_copy if unsubscribe_copy is not None else DEFAULT_UNSUBSCRIBE_COPY).strip(),
        (signature if signature is not None else DEFAULT_SIGNATURE).strip(),
    ]
    sections = [section for section in sections if section]
    if not sections:
        return ""
    if any(template_has_html(section) for section in sections):
        return "\n".join(
            section if template_has_html(section) else text_section_to_html(section)
            for section in sections
            if section
        ).strip()
    return "\n\n".join(sections).strip()


def body_to_html(body):
    text = body or ""
    if template_has_html(text):
        return text
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        return ""
    return "\n".join(f"<p>{escape(part).replace(chr(10), '<br>')}</p>" for part in paragraphs)


def log_unsent_dispatch_records(records, start_index, subject_template, body_template, variant, reason, already_successful=None):
    already_successful = already_successful or set()
    logged = 0
    for index, record in enumerate(records[start_index:], start=start_index):
        target_email = normalize_email(record.get("Email", ""))
        if not target_email or target_email in already_successful:
            continue
        company = clean_cell(record.get("Company"), "your team")
        icebreaker = f"I hope you and the team at {company} are doing well."
        rendered_subject = render_variant_template(
            subject_template,
            record,
            icebreaker,
            seed=f"skipped:{index}:subject",
        )
        rendered_body = render_variant_template(
            body_template,
            record,
            icebreaker,
            seed=f"skipped:{index}:body",
        )
        rendered_html = body_to_html(rendered_body)
        log_outbound(
            "",
            target_email,
            rendered_subject,
            rendered_html,
            variant,
            "skipped",
            plain_text=html_to_plain_text(rendered_html),
            target_domain=get_domain(target_email),
            error=reason,
        )
        logged += 1
    return logged


def sender_rows_one_per_domain(sender_rows):
    grouped = {}
    for sender in sender_rows:
        email = normalize_email(sender.get("email", ""))
        domain = get_domain(email)
        if domain:
            grouped.setdefault(domain, []).append(sender)
    return [random.choice(rows) for rows in grouped.values() if rows]


SENDER_IMPORT_COLUMNS = {
    "email": ["email", "sender_email", "邮箱", "发件箱", "发件邮箱"],
    "password": ["password", "app_password", "app password", "密码", "邮箱密码", "应用密码"],
    "daily_limit": ["daily_limit", "daily limit", "每日上限", "日上限", "发送上限"],
    "from_name": ["from_name", "from name", "发件人名", "发件人", "名称"],
    "smtp_host": ["smtp_host", "smtp host", "SMTP Host"],
    "smtp_port": ["smtp_port", "smtp port", "SMTP Port"],
    "imap_host": ["imap_host", "imap host", "IMAP Host"],
    "imap_port": ["imap_port", "imap port", "IMAP Port"],
    "reply_to_email": ["reply_to_email", "reply to", "reply-to", "回复邮箱"],
}


def _column_lookup(df):
    return {str(column).strip().lower(): column for column in df.columns}


def _find_column(df, field):
    lookup = _column_lookup(df)
    for candidate in SENDER_IMPORT_COLUMNS[field]:
        column = lookup.get(candidate.strip().lower())
        if column is not None:
            return column
    return None


def _optional_cell(row, column, default=""):
    if column is None:
        return default
    return clean_cell(row.get(column), default)


def _optional_int(row, column, default):
    value = _optional_cell(row, column, "")
    if not value:
        return default
    return int(float(value))


def sender_import_columns(df):
    return {field: _find_column(df, field) for field in SENDER_IMPORT_COLUMNS}


def query_rows(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def audit_filter_context(status="", sender="", receiver="", domain="", error_q=""):
    status = (status or "").strip().lower()
    if status == "skiped":
        status = "skipped"
    if status not in {"", "success", "failed", "skipped", "unsent"}:
        status = ""
    return {
        "status": status,
        "sender": (sender or "").strip().lower(),
        "receiver": (receiver or "").strip().lower(),
        "domain": (domain or "").strip().lower(),
        "error_q": (error_q or "").strip(),
    }


def audit_where_clause(filters, export_unsent=False):
    clauses = []
    params = []
    status = filters.get("status", "")
    if export_unsent:
        clauses.append("status IN (?, ?)")
        params.extend(sorted(AUDIT_EXPORT_STATUSES))
    elif status == "unsent":
        clauses.append("status IN (?, ?)")
        params.extend(sorted(AUDIT_EXPORT_STATUSES))
    elif status:
        clauses.append("status = ?")
        params.append(status)
    if filters.get("sender"):
        clauses.append("LOWER(COALESCE(sender, '')) LIKE ?")
        params.append(f"%{filters['sender']}%")
    if filters.get("receiver"):
        clauses.append("LOWER(COALESCE(receiver, '')) LIKE ?")
        params.append(f"%{filters['receiver']}%")
    if filters.get("domain"):
        clauses.append("LOWER(COALESCE(target_domain, '')) LIKE ?")
        params.append(f"%{filters['domain']}%")
    if filters.get("error_q"):
        clauses.append("LOWER(COALESCE(error, '')) LIKE ?")
        params.append(f"%{filters['error_q'].lower()}%")
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def audit_query_string(filters, page=None):
    query = {key: value for key, value in filters.items() if value}
    if page is not None:
        query["page"] = page
    encoded = urlencode(query)
    return f"?{encoded}" if encoded else ""


def email_test_gmail_from_auth(auth_data, request_data=None):
    request_data = request_data or {}
    return (
        request_data.get("target_email")
        or request_data.get("gmail_address")
        or auth_data.get("gmail_address")
        or auth_data.get("test_gmail")
        or auth_data.get("seed_email")
        or ""
    )


def _email_test_level(placement, status):
    status = str(status or "").lower()
    placement = str(placement or "").lower()
    if placement in {"inbox", "primary", "main", "promotions", "updates"}:
        return "success"
    if placement in {"spam", "junk"} or status in {"failed", "error"}:
        return "error"
    return "info"


def email_test_result_view(lang, result):
    if not result:
        return None
    request_id = result.get("emailtestrequestid") or result.get("request_id") or ""
    status = str(result.get("status") or "pending")
    placement = result.get("placement") or result.get("folder") or result.get("mailbox") or result.get("result") or ""
    sender_domain = result.get("sender_domain") or get_domain(result.get("sender_email") or result.get("sender") or "")
    return {
        "level": _email_test_level(placement, status),
        "sender": t(lang, "email_test_domain", domain=sender_domain) if sender_domain else "",
        "status": t(lang, "email_test_status", request_id=request_id, status=status),
        "placement": t(lang, "email_test_result", placement=placement) if placement else "",
        "request_id": request_id,
        "raw_status": status,
        "error": result.get("error", ""),
        "is_pending": status.lower() not in {"completed", "failed", "expired"},
    }


def email_test_results_view(lang, results):
    if not results:
        return []
    if isinstance(results, dict):
        results = [results]
    return [view for view in (email_test_result_view(lang, result) for result in results) if view]


def email_test_diagnostics_view(lang, diagnostics):
    if not diagnostics:
        return None
    status = str(diagnostics.get("status") or "")
    data = diagnostics.get("data") if isinstance(diagnostics.get("data"), dict) else diagnostics
    if status == "failed" or diagnostics.get("error"):
        return {
            "level": "error",
            "title": t(lang, "email_test_diagnostics_title"),
            "message": t(lang, "email_test_diagnostics_fail", error=diagnostics.get("error") or "unknown"),
        }
    scan = data.get("scan") if isinstance(data.get("scan"), dict) else {}
    message = (
        t(
            lang,
            "email_test_diagnostics_ok",
            pending=int(data.get("pending_count") or 0),
            completed=int(data.get("recent_completed_count") or 0),
            checked=int(scan.get("checked") or 0),
            matched=int(scan.get("matched") or 0),
        )
        if data.get("gmail_api_ok")
        else t(lang, "email_test_diagnostics_fail", error=data.get("gmail_error") or "not configured")
    )
    pending_refs = data.get("pending_refs") if isinstance(data.get("pending_refs"), list) else []
    if pending_refs:
        message = f"{message} Pending refs: {', '.join(str(item) for item in pending_refs[:5])}."
    return {
        "level": "success" if data.get("gmail_api_ok") else "warning",
        "title": t(lang, "email_test_diagnostics_title"),
        "message": message,
    }


def _merge_email_test_result(original, polled):
    merged = dict(original or {})
    merged.update(polled or {})
    sender = original.get("sender_email") or original.get("sender") if original else ""
    if sender:
        merged["sender_email"] = sender
    if not merged.get("request_id") and merged.get("emailtestrequestid"):
        merged["request_id"] = merged["emailtestrequestid"]
    if not merged.get("emailtestrequestid") and merged.get("request_id"):
        merged["emailtestrequestid"] = merged["request_id"]
    return merged


def refresh_email_test_results(token, results, wait=False):
    if isinstance(results, dict):
        results = [results]
    results = [dict(result) for result in (results or [])]
    deadline = time.time() + EMAIL_TEST_POLL_SECONDS if wait else time.time()

    while True:
        refreshed = []
        pending = False
        for result in results:
            request_id = result.get("emailtestrequestid") or result.get("request_id") or ""
            status = str(result.get("status") or "").lower()
            if request_id and status not in {"completed", "failed", "expired"}:
                try:
                    polled = poll_email_test_request(token, request_id)
                    result = _merge_email_test_result(result, polled)
                    status = str(result.get("status") or "").lower()
                except EmailTestApiError as exc:
                    message = str(exc)
                    if "not found" in message.lower():
                        result = _merge_email_test_result(
                            result,
                            {
                                "status": "expired",
                                "error": "This request is no longer available. Start a new placement test.",
                            },
                        )
                        status = "expired"
                    else:
                        result = _merge_email_test_result(result, {"status": "failed", "error": message})
                        status = "failed"
            pending = pending or status not in {"completed", "failed", "expired"}
            refreshed.append(result)

        results = refreshed
        if not wait or not pending or time.time() >= deadline:
            return results
        time.sleep(max(1, int(EMAIL_TEST_POLL_INTERVAL_SECONDS)))


def _report_match_key(report):
    return (
        str(report.get("sender_domain") or "").lower(),
        normalize_email(report.get("sender_email") or report.get("sender") or ""),
    )


def _clamp_score(score):
    return max(0, min(100, int(round(score or 0))))


def _score_level(score):
    return "success" if score >= 85 else "warning" if score >= 65 else "error"


def _domain_display_score_offset(domain):
    domain = str(domain or "").strip().lower()
    if not domain:
        return 0
    digest = hashlib.sha256(domain.encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 5) - 2


def _domain_sort_key(domain):
    domain = str(domain or "").strip().lower()
    return hashlib.sha256(domain.encode("utf-8")).hexdigest() if domain else ""


def _spread_duplicate_domain_scores(reports):
    groups = {}
    for index, report in enumerate(reports):
        domain = str(report.get("sender_domain") or "").strip().lower()
        if not domain:
            continue
        groups.setdefault(report.get("score"), []).append(index)

    for indices in groups.values():
        domains = {str(reports[index].get("sender_domain") or "").strip().lower() for index in indices}
        if len(domains) <= 1:
            continue
        used_scores = set()
        for index in sorted(indices, key=lambda item: _domain_sort_key(reports[item].get("sender_domain"))):
            score = _clamp_score(reports[index].get("score"))
            if score in used_scores:
                for delta in (1, -1, 2, -2, 3, -3, 4, -4):
                    candidate = _clamp_score(score + delta)
                    if candidate not in used_scores:
                        score = candidate
                        reports[index]["score"] = score
                        reports[index]["display_adjustment"] = int(reports[index].get("display_adjustment") or 0) + delta
                        break
            used_scores.add(score)
            reports[index]["level"] = _score_level(score)
    return reports


def _combine_email_test_reports(local_reports, backend_data):
    backend_reports = []
    if isinstance(backend_data, dict):
        backend_reports = backend_data.get("reports") or backend_data.get("results") or []
    if isinstance(backend_reports, dict):
        backend_reports = [backend_reports]
    backend_by_key = {_report_match_key(report): report for report in backend_reports if isinstance(report, dict)}

    reports = []
    for local in local_reports:
        key = _report_match_key(local)
        remote = backend_by_key.get(key) or backend_by_key.get((key[0], ""))
        if not remote:
            remote = {}
        local_categories = local.get("categories") if isinstance(local.get("categories"), list) else []
        remote_categories = remote.get("categories") if isinstance(remote.get("categories"), list) else []
        categories = remote_categories + local_categories
        categories = [category for category in categories if isinstance(category, dict)]
        scored_categories = [category for category in categories if isinstance(category.get("score"), (int, float))]
        base_score = _clamp_score(
            sum(category["score"] for category in scored_categories) / len(scored_categories)
            if scored_categories
            else local.get("score", 0)
        )
        findings = []
        for category in categories:
            for finding in category.get("findings") or []:
                findings.append(finding)
        if remote.get("error"):
            findings.insert(
                0,
                {
                    "code": "backend_domain_error",
                    "title": "Backend domain check failed",
                    "detail": str(remote.get("error")),
                    "severity": "warning",
                },
            )
        sender_domain = local.get("sender_domain") or remote.get("sender_domain") or key[0]
        display_adjustment = _domain_display_score_offset(sender_domain)
        score = _clamp_score(base_score + display_adjustment)
        reports.append(
            {
                "sender_email": local.get("sender_email") or remote.get("sender_email") or "",
                "sender_domain": sender_domain,
                "base_score": base_score,
                "display_adjustment": display_adjustment,
                "score": score,
                "level": _score_level(score),
                "categories": categories,
                "dangerous_words": local.get("dangerous_words") or [],
                "link_domains": local.get("link_domains") or [],
                "findings": findings,
                "backend": remote,
            }
        )
    reports = _spread_duplicate_domain_scores(reports)
    summary_score = round(sum(item["score"] for item in reports) / len(reports)) if reports else 0
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "score": summary_score,
        "level": _score_level(summary_score),
        "reports": reports,
    }


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return await warm_page(request)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(FAVICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/senders/template")
async def sender_template_download():
    headers = {"Content-Disposition": 'attachment; filename="senderemaillist.xlsx"'}
    if SENDER_TEMPLATE_PATH.exists():
        return FileResponse(
            SENDER_TEMPLATE_PATH,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="senderemaillist.xlsx",
        )
    return StreamingResponse(
        generate_sender_template_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@app.post("/settings/remarketing-cooldown")
async def save_remarketing_cooldown(request: Request, remarketing_cooldown_days: int = Form(REMARKETING_COOLDOWN_DAYS)):
    days = normalize_remarketing_cooldown_days(remarketing_cooldown_days)
    upsert_app_setting(REMARKETING_COOLDOWN_SETTING_KEY, str(days))
    wants_json = (
        request.headers.get("x-requested-with") == "fetch"
        or "application/json" in request.headers.get("accept", "")
    )
    if wants_json:
        return JSONResponse({"status": "saved", "days": days})
    flash(request, "success", t(get_lang(request), "remarketing_cooldown_saved"))
    return redirect("/dispatch#lead-section")


@app.post("/settings/proxy")
async def save_proxy_route(
    request: Request,
    proxy_enabled: str = Form(""),
    proxy_url: str = Form(""),
):
    lang = get_lang(request)
    enabled = proxy_enabled.strip().lower() in {"1", "true", "on", "yes"}
    try:
        save_proxy_settings(enabled, proxy_url)
    except ValueError as exc:
        flash(request, "error", t(lang, "proxy_invalid", error=str(exc)))
        return redirect("/config")
    flash(request, "success", t(lang, "proxy_saved"))
    return redirect("/config")


@app.get("/dispatch", response_class=HTMLResponse)
async def dispatch_page(request: Request, lead_page: int = 1):
    lang = get_lang(request)
    senders = list_senders()
    auth_data = _email_test_sync_auth_from_bff(request)
    auth_request = _email_test_auth_request(request)
    email_test_report = _email_test_cache_get(
        EMAIL_TEST_REPORT_CACHE,
        request.session.get("email_test_report_id", ""),
        request.session.get("email_test_report") or {},
    )
    email_test_analysis_job = request.session.get("email_test_analysis_job") or {}
    email_test_analysis_error = request.session.get("email_test_analysis_error") or ""
    lead_preview_df = get_cached_lead_dataframe(request)
    lead_preview = lead_preview_from_df(
        lead_preview_df,
        lang,
        filename=request.session.get("lead_preview_filename", ""),
        page=lead_page,
    )
    sample_df = await load_lead_dataframe(None)
    draft_subject = normalize_subject_template(request.session.get("draft_subject", DEFAULT_SUBJECT_TEMPLATE))
    request.session["draft_subject"] = draft_subject
    draft_body = request.session.get(
        "draft_body",
        (
            "Hi {Name},\n\n"
            "I am reaching out from ePetrel AI Studio with a concise collaboration idea for {Company}.\n\n"
            "Would it make sense to send a few examples?"
        ),
    )
    saved_unsubscribe = normalize_unsubscribe_copy(get_app_setting(UNSUBSCRIBE_COPY_SETTING_KEY, DEFAULT_UNSUBSCRIBE_COPY))
    saved_signature = get_app_setting(SIGNATURE_SETTING_KEY, DEFAULT_SIGNATURE)
    draft_unsubscribe = normalize_unsubscribe_copy(request.session.get("draft_unsubscribe", saved_unsubscribe))
    draft_signature = request.session.get("draft_signature", saved_signature)
    draft_full_body = compose_email_template(draft_body, draft_unsubscribe, draft_signature)
    preview_subject = render_variant_template(
        draft_subject,
        sample_df.iloc[0].to_dict(),
        "Preview icebreaker",
        seed="preview-subject",
    )
    preview_html = render_variant_template(
        body_to_html(draft_full_body),
        sample_df.iloc[0].to_dict(),
        "Preview icebreaker",
        seed="preview-body",
    )
    preflight = lint_email(preview_subject, preview_html, lang=lang)
    if (
        not validate_spintax_format(draft_subject)
        or not validate_spintax_format(draft_body)
        or not validate_spintax_format(draft_unsubscribe)
        or not validate_spintax_format(draft_signature)
    ):
        preflight.append(t(lang, "variant_format_error"))
    return templates.TemplateResponse(
        request=request,
        name="dispatch.html",
        context=page_context(
            request,
            "dispatch",
            "dispatch_title",
            "dispatch_caption",
            senders=senders,
            active_senders=list_senders(include_credentials=False),
            sample_leads=records_from_df(sample_df),
            lead_preview=lead_preview,
            remarketing_cooldown_days=get_remarketing_cooldown_days(),
            preview_subject=preview_subject,
            preview_html=preview_html,
            preflight=preflight,
            draft_subject=draft_subject,
            draft_body=draft_body,
            draft_unsubscribe=draft_unsubscribe,
            draft_signature=draft_signature,
            draft_full_body=draft_full_body,
            email_templates=list_email_templates(EMAIL_TEMPLATE_SLOT_COUNT),
            cold_email_word_min=COLD_EMAIL_WORD_MIN,
            cold_email_word_max=COLD_EMAIL_WORD_MAX,
            dangerous_terms=load_dangerous_words(),
            mail_provider_rows=MAIL_PROVIDER_ROWS,
            gmail_oauth_authorization_url=request.session.pop("gmail_oauth_authorization_url", ""),
            gmail_consumer_daily_limit=GMAIL_ACCOUNT_TYPE_DAILY_LIMITS["consumer_gmail"],
            gmail_workspace_daily_limit=GMAIL_ACCOUNT_TYPE_DAILY_LIMITS["workspace_gmail"],
            available_sender_count=len(get_active_senders()),
            active_seed_count=len(list_seed_accounts(active_only=True)),
            auth_data=auth_data,
            auth_request=auth_request,
            auth_email=(
                (auth_data.get("user") or {}).get("email")
                if isinstance(auth_data.get("user"), dict)
                else auth_data.get("email", "")
            ),
            auth_gmail=email_test_gmail_from_auth(auth_data),
            email_test_report=email_test_report,
            email_test_analysis_job=email_test_analysis_job,
            email_test_analysis_error=email_test_analysis_error,
            email_test_results=[],
            email_test_has_pending=False,
            email_test_auto_poll=False,
            email_test_auto_poll_paused=False,
            email_test_diagnostics=email_test_diagnostics_view(lang, request.session.get("email_test_diagnostics") or {}),
            epetrel_url=EPETREL_SITE_URL.rstrip("/") + "/",
        ),
    )


@app.post("/senders")
async def save_sender(
    request: Request,
    sender_email: str = Form(""),
    sender_password: str = Form(""),
    auth_method: str = Form("smtp"),
    gmail_account_type: str = Form(""),
    daily_limit: int = Form(DEFAULT_DAILY_LIMIT),
    from_name: str = Form("MutualWarm"),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
):
    lang = get_lang(request)
    normalized = normalize_email(sender_email)
    if auth_method == "gmail_api":
        flash(request, "info", t(lang, "gmail_api_hint"))
        return redirect("/config")
    if not normalized or not sender_password or not from_name.strip() or not smtp_host.strip() or not imap_host.strip() or int(smtp_port or 0) <= 0 or int(imap_port or 0) <= 0:
        flash(request, "error", t(lang, "valid_sender_error"))
    else:
        check_result = check_sender_mailbox(
            normalized,
            sender_password,
            smtp_host.strip(),
            int(smtp_port),
            imap_host.strip(),
            int(imap_port),
        )
        upsert_sender(
            normalized,
            sender_password,
            daily_limit=int(daily_limit),
            from_name=from_name,
            smtp_host=smtp_host.strip(),
            smtp_port=int(smtp_port),
            imap_host=imap_host.strip(),
            imap_port=int(imap_port),
            smtp_check_status=check_result["smtp"],
            imap_check_status=check_result["imap"],
            mailbox_check_status=check_result["mailbox"],
            check_error=check_result["error"],
            auth_method="smtp",
            gmail_client_id="",
            gmail_client_secret="",
            gmail_refresh_token="",
            gmail_token_status="not_connected",
            gmail_granted_scopes="",
            gmail_account_type="smtp_generic",
        )
        if check_result["mailbox"] == "passed":
            flash(request, "success", f"{t(lang, 'saved_sender', email=normalized)}. {t(lang, 'sender_check_passed')}")
        else:
            flash(request, "warning", t(lang, "sender_check_failed", email=normalized, error=check_result["error"] or "unknown"))
    return redirect("/config")


@app.post("/gmail/oauth/start")
async def gmail_oauth_start(
    request: Request,
    sender_email: str = Form(""),
    sender_password: str = Form(""),
    daily_limit: int = Form(DEFAULT_DAILY_LIMIT),
    from_name: str = Form("MutualWarm"),
    gmail_account_type: str = Form("workspace_gmail"),
    imap_host: str = Form("imap.gmail.com"),
    imap_port: int = Form(993),
    gmail_client_id: str = Form(""),
    gmail_client_secret: str = Form(""),
    gmail_modify_scope: str = Form(""),
    oauth_action: str = Form("redirect"),
):
    lang = get_lang(request)
    normalized = normalize_email(sender_email)
    account_type = normalize_gmail_account_type(gmail_account_type, normalized)
    client_id = (gmail_client_id or "").strip()
    client_secret = (gmail_client_secret or "").strip()
    if not normalized or not from_name.strip() or int(daily_limit or 0) <= 0 or not client_id or not client_secret:
        flash(request, "error", t(lang, "gmail_api_missing_config"))
        return redirect("/config")
    if account_type == "consumer_gmail" and get_domain(normalized) not in {"gmail.com", "googlemail.com"}:
        flash(request, "error", t(lang, "gmail_api_missing_config"))
        return redirect("/config")

    normalized_daily_limit = int(daily_limit)
    if account_type == "consumer_gmail" and normalized_daily_limit == int(DEFAULT_DAILY_LIMIT):
        normalized_daily_limit = gmail_account_default_daily_limit(account_type)

    requested_scopes = list(GMAIL_FULL_AUTO_WARM_SCOPES)

    state = f"gmail_{uuid.uuid4()}"
    redirect_uri = str(request.url_for("gmail_oauth_callback"))
    GMAIL_OAUTH_PENDING[state] = {
        "expires_at": time.time() + 10 * 60,
        "email": normalized,
        "password": sender_password or "",
        "daily_limit": normalized_daily_limit,
        "from_name": from_name.strip(),
        "gmail_account_type": account_type,
        "imap_host": (imap_host or "imap.gmail.com").strip(),
        "imap_port": int(imap_port or 993),
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "requested_scopes": requested_scopes,
    }
    try:
        authorization_url, code_verifier = build_gmail_oauth_url(
            client_id,
            client_secret,
            redirect_uri,
            state,
            login_hint=normalized,
            scopes=requested_scopes,
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
        gmail_oauth_logger.warning("gmail oauth callback missing code email=%s state=%s", mask_email(pending.get("email", "")), state[:18])
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
            scopes=pending.get("requested_scopes") or GMAIL_BASE_SCOPES,
        )
        refresh_token = credentials.refresh_token
        if not refresh_token:
            raise RuntimeError("Google did not return a refresh token. Revoke the app grant in your Google account, then reconnect.")
        granted_scopes = sorted(set(credentials.granted_scopes or credentials.scopes or pending.get("requested_scopes") or GMAIL_BASE_SCOPES))
        profile = fetch_gmail_profile(credentials.token)
        authorized_email = normalize_email(profile.get("emailAddress", ""))
        expected_email = normalize_email(pending["email"])
        if not authorized_email or authorized_email != expected_email:
            gmail_oauth_logger.warning(
                "gmail oauth email mismatch expected=%s actual=%s state=%s",
                mask_email(expected_email),
                mask_email(authorized_email),
                state[:18],
            )
            flash(
                request,
                "error",
                t(lang, "gmail_oauth_email_mismatch", actual=authorized_email or "unknown", expected=expected_email),
            )
            return redirect("/config")

        imap_check_status = "unchecked"
        mailbox_check_status = "passed"
        check_error = ""
        if pending.get("password") and pending.get("imap_host"):
            imap_result = check_imap_login(
                pending["email"],
                pending["password"],
                pending["imap_host"],
                pending["imap_port"],
            )
            imap_check_status = imap_result["status"]
            check_error = imap_result["error"]
            if imap_result["status"] != "passed":
                mailbox_check_status = "api_connected"

        upsert_sender(
            pending["email"],
            pending.get("password", ""),
            daily_limit=pending["daily_limit"],
            from_name=pending["from_name"],
            smtp_host="gmail.googleapis.com",
            smtp_port=443,
            imap_host=pending["imap_host"],
            imap_port=pending["imap_port"],
            smtp_check_status="passed",
            imap_check_status=imap_check_status,
            mailbox_check_status=mailbox_check_status,
            check_error=check_error,
            auth_method="gmail_api",
            gmail_client_id=pending["client_id"],
            gmail_client_secret=pending["client_secret"],
            gmail_refresh_token=refresh_token,
            gmail_token_status="connected",
            gmail_granted_scopes=" ".join(granted_scopes),
            gmail_account_type=pending.get("gmail_account_type") or normalize_gmail_account_type("", pending["email"]),
        )
        gmail_oauth_logger.info("gmail oauth connected email=%s state=%s", mask_email(pending["email"]), state[:18])
        if GMAIL_MODIFY_SCOPE in set(pending.get("requested_scopes") or []) and GMAIL_MODIFY_SCOPE not in granted_scopes:
            flash(request, "warning", t(lang, "gmail_api_connected_limited", email=pending["email"]))
        else:
            flash(request, "success", t(lang, "gmail_api_connected", email=pending["email"]))
    except Exception as exc:
        gmail_oauth_logger.exception("gmail oauth callback failed email=%s state=%s error=%s", mask_email(pending.get("email", "")), state[:18], redact_sensitive(str(exc)))
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


@app.post("/senders/import")
async def import_senders(
    request: Request,
    senders_file: UploadFile = File(None),
    import_check_login: str = Form(""),
):
    lang = get_lang(request)
    df = await load_uploaded_dataframe(senders_file)
    if df is None:
        flash(request, "error", t(lang, "sender_import_missing_file"))
        return redirect("/config")

    columns = sender_import_columns(df)
    if any(not columns[field] for field in REQUIRED_SENDER_FIELDS):
        flash(request, "error", t(lang, "sender_import_missing_cols"))
        return redirect("/config")

    imported = 0
    errors = []
    should_check = bool(import_check_login)
    for index, row in df.iterrows():
        row_number = int(index) + 2
        try:
            email = normalize_email(_optional_cell(row, columns["email"]))
            password = _optional_cell(row, columns["password"])
            daily_limit_raw = _optional_cell(row, columns["daily_limit"], "")
            smtp_host = _optional_cell(row, columns["smtp_host"], "")
            smtp_port_raw = _optional_cell(row, columns["smtp_port"], "")
            imap_host = _optional_cell(row, columns["imap_host"], "")
            imap_port_raw = _optional_cell(row, columns["imap_port"], "")
            from_name = _optional_cell(row, columns["from_name"], "")
            if not all([email, password, daily_limit_raw, from_name, smtp_host, smtp_port_raw, imap_host, imap_port_raw]):
                raise ValueError(t(lang, "sender_import_missing_required"))
            if not email or not password:
                raise ValueError(t(lang, "valid_sender_error"))

            daily_limit = int(float(daily_limit_raw))
            smtp_port = int(float(smtp_port_raw))
            imap_port = int(float(imap_port_raw))
            reply_to_email = normalize_email(_optional_cell(row, columns["reply_to_email"], "")) or None

            check_result = {"smtp": "unchecked", "imap": "unchecked", "mailbox": "unchecked", "error": ""}
            if should_check:
                check_result = check_sender_mailbox(
                    email,
                    password,
                    smtp_host,
                    smtp_port,
                    imap_host,
                    imap_port,
                )

            upsert_sender(
                email,
                password,
                daily_limit=daily_limit,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                imap_host=imap_host,
                imap_port=imap_port,
                from_name=from_name,
                reply_to_email=reply_to_email,
                smtp_check_status=check_result["smtp"],
                imap_check_status=check_result["imap"],
                mailbox_check_status=check_result["mailbox"],
                check_error=check_result["error"],
            )
            imported += 1
        except Exception as exc:
            errors.append(t(lang, "sender_import_row_error", row=row_number, error=str(exc)))

    level = "success" if imported and not errors else "warning" if imported else "error"
    flash(request, level, t(lang, "sender_import_done", count=imported, failed=len(errors)))
    for error in errors[:5]:
        flash(request, "warning", error)
    return redirect("/config")


@app.post("/leads/preview")
async def preview_leads(
    request: Request,
    leads_file: UploadFile = File(None),
    subject: str = Form(DEFAULT_SUBJECT_TEMPLATE),
    html_body: str = Form(""),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
):
    lang = get_lang(request)
    subject = normalize_subject_template(subject)
    unsubscribe_copy = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_subject"] = subject
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = unsubscribe_copy
    request.session["draft_signature"] = signature
    df = await load_lead_dataframe(leads_file, allow_sample=False)
    if df is None:
        filename = (leads_file.filename or "") if leads_file else ""
        if filename:
            clear_lead_preview(request)
        flash(request, "error", t(lang, "lead_file_unsupported" if filename else "lead_file_missing"))
        return redirect("/dispatch#lead-section")
    if "Email" not in df.columns:
        clear_lead_preview(request)
        flash(request, "error", t(lang, "missing_email_col"))
        return redirect("/dispatch#lead-section")

    valid = count_valid_leads(df)
    if valid <= 0:
        clear_lead_preview(request)
        flash(request, "error", t(lang, "lead_no_valid"))
        return redirect("/dispatch#lead-section")

    preview_id = f"lead_{uuid.uuid4()}"
    clear_lead_preview(request)
    request.session["lead_preview_id"] = preview_id
    persist_lead_preview(request, df, leads_file.filename if leads_file else "")
    imported = upsert_crm_contacts_from_records(records_from_df(df), max_attempts=get_crm_max_remarketing_attempts())
    flash(request, "success", t(lang, "lead_preview_done", rows=len(df), valid=valid))
    flash(request, "success", t(lang, "crm_import_done", count=imported))
    return redirect("/dispatch#lead-section")


@app.post("/leads/preview/delete")
async def delete_preview_lead(
    request: Request,
    row_number: int = Form(0),
    lead_page: int = Form(1),
    subject: str = Form(DEFAULT_SUBJECT_TEMPLATE),
    html_body: str = Form(""),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
):
    lang = get_lang(request)
    subject = normalize_subject_template(subject)
    unsubscribe_copy = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_subject"] = subject
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = unsubscribe_copy
    request.session["draft_signature"] = signature
    df = get_cached_lead_dataframe(request)
    if df is None or df.empty:
        flash(request, "warning", t(lang, "lead_preview_missing"))
        return redirect("/dispatch#lead-section")

    index = int(row_number or 0) - 1
    if index < 0 or index >= len(df):
        flash(request, "warning", t(lang, "lead_preview_missing"))
        return redirect("/dispatch#lead-section")

    updated_df = df.drop(df.index[index]).reset_index(drop=True)
    if updated_df.empty:
        clear_lead_preview(request)
        flash(request, "success", t(lang, "deleted_lead", row=row_number))
        return redirect("/dispatch#lead-section")

    persist_lead_preview(request, updated_df, request.session.get("lead_preview_filename", ""))
    flash(request, "success", t(lang, "deleted_lead", row=row_number))
    target_pages = max(1, (len(updated_df) + LEAD_PREVIEW_PAGE_SIZE - 1) // LEAD_PREVIEW_PAGE_SIZE)
    target_page = max(1, min(int(lead_page or 1), target_pages))
    return redirect(f"/dispatch?lead_page={target_page}#lead-section")


@app.post("/leads/preview/clear")
async def clear_preview_leads(
    request: Request,
    subject: str = Form(DEFAULT_SUBJECT_TEMPLATE),
    html_body: str = Form(""),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
):
    lang = get_lang(request)
    request.session["draft_subject"] = normalize_subject_template(subject)
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_signature"] = signature
    clear_lead_preview(request)
    flash(request, "success", t(lang, "cleared_leads"))
    return redirect("/dispatch#lead-section")


@app.get("/leads/status")
async def lead_send_status(request: Request):
    df = get_cached_lead_dataframe(request)
    if df is None or "Email" not in df.columns:
        return JSONResponse({"sent": [], "total": 0})
    emails = [normalize_email(value) for value in df["Email"]]
    sent = sorted(list_successful_receivers(emails))
    return JSONResponse({"sent": sent, "total": len(emails)})


@app.post("/template-defaults/unsubscribe")
async def save_unsubscribe_default(
    request: Request,
    subject: str = Form(DEFAULT_SUBJECT_TEMPLATE),
    html_body: str = Form(""),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
):
    lang = get_lang(request)
    subject = normalize_subject_template(subject)
    unsubscribe_copy = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_subject"] = subject
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = unsubscribe_copy
    request.session["draft_signature"] = signature
    if not validate_spintax_format(unsubscribe_copy):
        flash(request, "error", t(lang, "variant_format_error"))
        return redirect("/dispatch#content-section")
    upsert_app_setting(UNSUBSCRIBE_COPY_SETTING_KEY, unsubscribe_copy)
    flash(request, "success", t(lang, "unsubscribe_copy_saved"))
    return redirect("/dispatch#content-section")


@app.post("/template-defaults/signature")
async def save_signature_default(
    request: Request,
    subject: str = Form(DEFAULT_SUBJECT_TEMPLATE),
    html_body: str = Form(""),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
):
    lang = get_lang(request)
    subject = normalize_subject_template(subject)
    unsubscribe_copy = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_subject"] = subject
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = unsubscribe_copy
    request.session["draft_signature"] = signature
    if not validate_spintax_format(signature):
        flash(request, "error", t(lang, "variant_format_error"))
        return redirect("/dispatch#content-section")
    upsert_app_setting(SIGNATURE_SETTING_KEY, signature)
    flash(request, "success", t(lang, "signature_saved"))
    return redirect("/dispatch#content-section")


@app.post("/email-templates/save")
async def save_email_template(
    request: Request,
    template_slot: int = Form(0),
    subject: str = Form(DEFAULT_SUBJECT_TEMPLATE),
    html_body: str = Form(""),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
):
    lang = get_lang(request)
    slot = max(1, min(int(template_slot or 1), EMAIL_TEMPLATE_SLOT_COUNT))
    form = await request.form()
    template_name = str(form.get(f"template_name_{slot}") or "").strip()
    subject = normalize_subject_template(subject)
    unsubscribe_copy = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_subject"] = subject
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = unsubscribe_copy
    request.session["draft_signature"] = signature
    if (
        not validate_spintax_format(subject)
        or not validate_spintax_format(html_body)
        or not validate_spintax_format(unsubscribe_copy)
        or not validate_spintax_format(signature)
    ):
        flash(request, "error", t(lang, "variant_format_error"))
        return redirect("/dispatch#content-section")
    upsert_email_template(slot, template_name, subject, html_body, unsubscribe_copy, signature)
    flash(request, "success", t(lang, "template_saved", slot=slot))
    return redirect("/dispatch#content-section")


@app.post("/email-templates/load")
async def load_email_template(
    request: Request,
    template_slot: int = Form(0),
):
    lang = get_lang(request)
    slot = max(1, min(int(template_slot or 1), EMAIL_TEMPLATE_SLOT_COUNT))
    template = get_email_template(slot)
    if not template:
        flash(request, "warning", t(lang, "template_missing"))
        return redirect("/dispatch#content-section")
    request.session["draft_subject"] = normalize_subject_template(template.get("subject") or DEFAULT_SUBJECT_TEMPLATE)
    request.session["draft_body"] = template.get("body") or ""
    request.session["draft_unsubscribe"] = normalize_unsubscribe_copy(template.get("unsubscribe_copy") or DEFAULT_UNSUBSCRIBE_COPY)
    request.session["draft_signature"] = template.get("signature") or DEFAULT_SIGNATURE
    flash(request, "success", t(lang, "template_loaded", slot=slot))
    return redirect("/dispatch#content-section")


@app.post("/email-templates/delete")
async def delete_saved_email_template(
    request: Request,
    template_slot: int = Form(0),
    subject: str = Form(DEFAULT_SUBJECT_TEMPLATE),
    html_body: str = Form(""),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
):
    lang = get_lang(request)
    slot = max(1, min(int(template_slot or 1), EMAIL_TEMPLATE_SLOT_COUNT))
    unsubscribe_copy = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_subject"] = normalize_subject_template(subject)
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = unsubscribe_copy
    request.session["draft_signature"] = signature
    if delete_email_template(slot):
        flash(request, "success", t(lang, "template_deleted", slot=slot))
    else:
        flash(request, "warning", t(lang, "template_missing"))
    return redirect("/dispatch#content-section")


@app.post("/variants/generate")
async def ai_generate_variants(
    request: Request,
    subject: str = Form(DEFAULT_SUBJECT_TEMPLATE),
    html_body: str = Form(""),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
):
    lang = get_lang(request)
    subject = normalize_subject_template(subject)
    unsubscribe_copy = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_subject"] = subject
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = unsubscribe_copy
    request.session["draft_signature"] = signature
    if (
        not validate_spintax_format(subject)
        or not validate_spintax_format(html_body)
        or not validate_spintax_format(unsubscribe_copy)
        or not validate_spintax_format(signature)
    ):
        flash(request, "error", t(lang, "variant_format_error"))
        return redirect("/config")

    try:
        generated_body = generate_copy_variants(html_body)
    except Exception as exc:
        email_test_logger.exception("copy optimization failed: %s", exc)
        generated_body = ""

    if (
        not generated_body
        or not validate_spintax_format(generated_body)
        or not contains_spintax_variants(generated_body)
        or normalize_copy_for_compare(generated_body) == normalize_copy_for_compare(html_body)
    ):
        email_test_logger.warning(
            "copy optimization rejected generated_len=%s valid=%s has_spintax=%s same_as_input=%s",
            len(generated_body or ""),
            validate_spintax_format(generated_body) if generated_body else False,
            contains_spintax_variants(generated_body) if generated_body else False,
            normalize_copy_for_compare(generated_body) == normalize_copy_for_compare(html_body) if generated_body else False,
        )
        flash(request, "error", t(lang, "variant_generate_failed"))
        return redirect("/config")

    request.session["draft_body"] = generated_body
    flash(request, "success", t(lang, "variant_generated"))
    return redirect("/config")


@app.post("/dispatch/stop")
async def stop_dispatch_queue(request: Request):
    DISPATCH_STOP_REQUESTS.add(dispatch_client_id(request))
    return JSONResponse({"status": "stopping"})


@app.post("/dispatch/send")
async def start_dispatch_queue(
    request: Request,
    leads_file: UploadFile = File(None),
    subject: str = Form(DEFAULT_SUBJECT_TEMPLATE),
    html_body: str = Form(
        "<p>I am reaching out from ePetrel AI Studio with a concise collaboration idea for {Company}.</p><p>Would it make sense to send a few examples?</p>"
    ),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
    delay_min: int = Form(60),
    delay_max: int = Form(180),
    remarketing_cooldown_days: int = Form(REMARKETING_COOLDOWN_DAYS),
    use_ai: str = Form(""),
    variant: str = Form("Variant-A"),
    mix_seed: str = Form(""),
    seed_interval: int = Form(10),
):
    lang = get_lang(request)
    client_id = dispatch_client_id(request)
    DISPATCH_STOP_REQUESTS.discard(client_id)
    subject = normalize_subject_template(subject)
    unsubscribe_copy = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_subject"] = subject
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = unsubscribe_copy
    request.session["draft_signature"] = signature
    remarketing_cooldown_days = normalize_remarketing_cooldown_days(remarketing_cooldown_days)
    upsert_app_setting(REMARKETING_COOLDOWN_SETTING_KEY, str(remarketing_cooldown_days))
    if (
        not validate_spintax_format(subject)
        or not validate_spintax_format(html_body)
        or not validate_spintax_format(unsubscribe_copy)
        or not validate_spintax_format(signature)
    ):
        flash(request, "error", t(lang, "variant_format_error"))
        return redirect("/config")

    if leads_file is not None and leads_file.filename:
        df = await load_lead_dataframe(leads_file)
        if df is not None:
            persist_lead_preview(request, df, leads_file.filename)
    else:
        df = get_cached_lead_dataframe(request)
        if df is None:
            flash(request, "error", t(lang, "lead_file_missing"))
            return redirect("/dispatch#lead-section")
    if df is None or "Email" not in df.columns:
        flash(request, "error", t(lang, "missing_email_col"))
        return redirect("/config")

    records = df.to_dict(orient="records")
    if not records or count_valid_leads(df) == 0:
        flash(request, "error", t(lang, "missing_email_col"))
        return redirect("/config")
    upsert_crm_contacts_from_records(records, max_attempts=get_crm_max_remarketing_attempts())

    full_body_template = compose_email_template(html_body, unsubscribe_copy, signature)
    missing_variables, empty_variable_rows = template_variable_errors(df, subject, full_body_template)
    if missing_variables or empty_variable_rows:
        if missing_variables:
            flash(request, "error", t(lang, "template_variable_missing_cols", columns=", ".join(missing_variables)))
        for item in empty_variable_rows[:8]:
            flash(
                request,
                "error",
                t(
                    lang,
                    "template_variable_empty_values",
                    details=f"Excel row {item['row']}: {', '.join(item['variables'])}",
                ),
            )
        if len(empty_variable_rows) > 8:
            flash(
                request,
                "warning",
                t(
                    lang,
                    "template_variable_empty_values",
                    details=f"+{len(empty_variable_rows) - 8} more rows",
                ),
            )
        return redirect("/dispatch#lead-section")

    active_seeds = list_seed_accounts(active_only=True)
    delay_min, delay_max = min(delay_min, delay_max), max(delay_min, delay_max)
    results = []
    sender_sequences = {}
    body_template = full_body_template
    first_remarketing_template = get_remarketing_template(1) or {}
    initial_followup_days = int(first_remarketing_template.get("cooldown_days") or remarketing_cooldown_days or 0)
    recently_successful = list_recent_successful_receivers(
        (normalize_email(record.get("Email", "")) for record in records),
        days=remarketing_cooldown_days,
    )

    for idx, record in enumerate(records):
        if dispatch_stop_requested(request):
            results.append(t(lang, "dispatch_stopped"))
            break

        target_email = normalize_email(record.get("Email", ""))
        if not target_email:
            results.append(f"Row {idx + 1}: invalid email skipped.")
            continue
        if target_email in recently_successful:
            results.append(f"Sent to {target_email} within the last {remarketing_cooldown_days} days; skipped duplicate.")
            continue
        if crm_contact_auto_excluded(target_email):
            results.append(f"Skipped {target_email}: CRM status excludes automatic remarketing.")
            continue

        sender_pool = get_active_senders(get_domain(target_email))
        if not sender_pool:
            reason = "No healthy sender is available, or every sender has reached its daily limit."
            logged = log_unsent_dispatch_records(
                records,
                idx,
                subject,
                body_template,
                variant,
                reason,
                already_successful=recently_successful,
            )
            results.append(f"{reason} Queue stopped. Logged {logged} unsent recipients as skipped.")
            break

        current_sender, current_pwd = sender_pool[0]
        sender_sequences[current_sender] = sender_sequences.get(current_sender, 0) + 1
        sequence_no = sender_sequences[current_sender]
        company = lead_field_value(record, "company") or clean_cell(record.get("Company"), "your team")
        icebreaker = (
            generate_icebreaker(lead_field_value(record, "company_bio") or clean_cell(record.get("Company_Bio")), lead_field_value(record, "position") or clean_cell(record.get("Position")))
            if use_ai
            else f"I hope you and the team at {company} are doing well."
        )

        final_subject = render_variant_template(
            subject,
            record,
            icebreaker,
            seed=f"{current_sender}:{sequence_no}:subject",
        )
        final_body = render_variant_template(
            body_template,
            record,
            icebreaker,
            seed=f"{current_sender}:{sequence_no}:body",
        )
        final_html = body_to_html(final_body)
        final_plain = html_to_plain_text(final_html)
        result = send_cold_email(
            current_sender,
            current_pwd,
            target_email,
            final_subject,
            final_html,
            final_plain,
            variant,
            crm_remarketing_step=0,
            crm_template_name="Initial Dispatch",
        )

        if result["status"] == "success":
            recently_successful.add(target_email)
            mark_crm_outbound(
                target_email,
                outbound_log_id=result.get("log_id") or 0,
                sender=current_sender,
                subject=final_subject,
                body_html=final_html,
                variant_version=variant,
                remarketing_step=0,
                template_name="Initial Dispatch",
                next_followup_at=crm_next_followup_at(initial_followup_days),
            )
            results.append(f"Sent via {current_sender} to {target_email}.")
        elif result["status"] == "skipped":
            results.append(f"Skipped {target_email}: {result['error']}")
        else:
            results.append(f"Delivery failed for {target_email}: {result['error']}")

        if mix_seed and active_seeds and result["status"] == "success" and (idx + 1) % int(seed_interval or 10) == 0:
            seed = active_seeds[((idx + 1) // int(seed_interval or 10) - 1) % len(active_seeds)]
            seed_result = send_cold_email(
                current_sender,
                current_pwd,
                seed["email"],
                final_subject,
                final_html,
                final_plain,
                f"{variant}-seed",
            )
            results.append(f"Seed placement test to {seed['email']}: {seed_result['status']}")

        if idx < len(records) - 1 and delay_max > 0:
            await dispatch_sleep(request, calculate_dispatch_delay(delay_min, delay_max, idx + 1))

    was_stopped = dispatch_stop_requested(request)
    DISPATCH_STOP_REQUESTS.discard(client_id)
    flash(request, "warning" if was_stopped else "success", t(lang, "dispatch_stopped" if was_stopped else "batch_done"))
    request.session["last_dispatch_results"] = results[-25:]
    return redirect("/config")


@app.post("/email-test/auth/start")
async def email_test_auth_start(request: Request):
    lang = get_lang(request)
    try:
        auth_data = _email_test_auth(request)
        wants_json = (
            request.headers.get("x-requested-with") == "fetch"
            or "application/json" in request.headers.get("accept", "")
        )
        if _email_test_auth_is_authorized(auth_data):
            if wants_json:
                return JSONResponse({"status": "authorized", "device_code": ""})
            flash(request, "success", t(lang, "email_test_authorized"))
            return redirect(EMAIL_TEST_SECTION)
        auth_request = start_warm_auth()
        request.session["warm_auth_request"] = auth_request
        request.session["warm_auth_started_at"] = time.time()
        request.session["email_test_result"] = {}
        request.session["email_test_results"] = []
        device_code = auth_request.get("device_code", "")
        if _email_test_auth_is_authorized(auth_request):
            _warm_store_auth(request, auth_request, device_code)
        login_url = auth_request.get("login_url")
        if wants_json:
            if _email_test_auth_is_authorized(auth_request):
                return JSONResponse({"status": "authorized", "device_code": device_code})
            return JSONResponse(
                {
                    "status": "started",
                    "login_url": login_url,
                    "device_code": device_code,
                }
            )
        if _email_test_auth_is_authorized(auth_request):
            flash(request, "success", t(lang, "email_test_authorized"))
            return redirect(EMAIL_TEST_SECTION)
        if login_url:
            return RedirectResponse(login_url, status_code=303)
    except WarmApiError as exc:
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=502)
        flash(request, "error", t(lang, "email_test_error", error=str(exc)))
    return redirect(EMAIL_TEST_SECTION)


@app.get("/email-test/auth/status")
async def email_test_auth_status(request: Request):
    lang = get_lang(request)
    auth_data = _email_test_auth(request)
    if _email_test_auth_is_authorized(auth_data):
        return JSONResponse({"status": "authorized"})

    auth_request = _email_test_auth_request(request)
    device_code = request.query_params.get("device_code") or auth_request.get("device_code", "")
    if not device_code:
        return JSONResponse({"status": "not_started"})

    cached_auth = _email_test_cache_get(EMAIL_TEST_AUTH_CACHE, device_code)
    if _email_test_auth_is_authorized(cached_auth):
        _warm_store_auth(request, cached_auth, device_code)
        return JSONResponse({"status": "authorized"})

    try:
        polled = poll_warm_auth(device_code)
        if _email_test_auth_is_authorized(polled):
            _warm_store_auth(request, polled, device_code)
            return JSONResponse({"status": "authorized"})
        started_at = float(request.session.get("warm_auth_started_at") or 0)
        elapsed_seconds = time.time() - started_at if started_at else 0
        if elapsed_seconds >= 120:
            email_test_logger.warning(
                "email test auth still pending after %.0fs device_code=%s",
                elapsed_seconds,
                device_code[:16],
            )
            return JSONResponse(
                {
                    "status": "stalled",
                    "error": t(lang, "email_test_auth_stalled"),
                }
            )
        return JSONResponse({"status": "pending"})
    except WarmApiError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=502)


@app.get("/email-test/auth/complete")
async def email_test_auth_complete(request: Request):
    lang = get_lang(request)
    auth_request = _email_test_auth_request(request)
    device_code = request.query_params.get("device_code") or auth_request.get("device_code", "")
    try:
        polled = {}
        if device_code:
            for attempt in range(4):
                polled = poll_warm_auth(device_code)
                if _email_test_auth_is_authorized(polled):
                    break
                if attempt < 3:
                    time.sleep(1)
        if _email_test_auth_is_authorized(polled):
            _warm_store_auth(request, polled, device_code)
            return HTMLResponse(
                f"""
                <!doctype html><html><head><meta charset="utf-8"><title>ePetrel Authorized</title></head>
                <body style="font-family:system-ui;padding:32px;color:#0b1c30;">
                  <h2>ePetrel login completed</h2>
                  <p>You can return to the ePetrel AI Dispatch System tab.</p>
                  <script>
                    const authMessage = {{
                      type: "epetrel-email-test-authorized",
                      deviceCode: {json.dumps(device_code)},
                      at: Date.now()
                    }};
                    try {{
                      localStorage.setItem("epetrel-email-test-auth-event", JSON.stringify(authMessage));
                      if (window.opener) {{
                        window.opener.postMessage(authMessage, window.location.origin);
                      }}
                    }} catch (error) {{}}
                    setTimeout(function(){{ window.close(); }}, 350);
                  </script>
                </body></html>
                """
            )
        else:
            email_test_logger.info(
                "email test auth complete still pending device_code=%s status=%s keys=%s",
                device_code[:16],
                polled.get("status", "") if isinstance(polled, dict) else "",
                sorted(polled.keys()) if isinstance(polled, dict) else [],
            )
            return HTMLResponse(
                f"""
                <!doctype html><html><head><meta charset="utf-8"><title>ePetrel Authorization</title></head>
                <body style="font-family:system-ui;padding:32px;color:#0b1c30;">
                  <h2>Finalizing ePetrel login</h2>
                  <p>Please keep this tab open for a moment.</p>
                  <script>
                    const deviceCode = {json.dumps(device_code)};
                    const notify = (type) => {{
                      try {{
                        const authMessage = {{ type, deviceCode, at: Date.now() }};
                        localStorage.setItem("epetrel-email-test-auth-event", JSON.stringify(authMessage));
                        if (window.opener) {{
                          window.opener.postMessage(authMessage, window.location.origin);
                        }}
                      }} catch (error) {{}}
                    }};
                    const finish = () => {{
                      notify("epetrel-email-test-authorized");
                      setTimeout(function(){{ window.close(); }}, 350);
                    }};
                    const poll = () => {{
                      if (!deviceCode) {{
                        notify("epetrel-email-test-pending");
                        return;
                      }}
                      fetch("/email-test/auth/status?device_code=" + encodeURIComponent(deviceCode), {{
                        credentials: "same-origin"
                      }})
                        .then((response) => response.json())
                        .then((payload) => {{
                          if (payload.status === "authorized") {{
                            finish();
                          }} else {{
                            notify("epetrel-email-test-pending");
                            setTimeout(poll, 900);
                          }}
                        }})
                        .catch(() => setTimeout(poll, 1200));
                    }};
                    poll();
                  </script>
                </body></html>
                """
            )
    except WarmApiError as exc:
        flash(request, "error", t(lang, "email_test_error", error=str(exc)))
    return redirect(EMAIL_TEST_SECTION)


@app.post("/email-test/auth/poll")
async def email_test_auth_poll(request: Request):
    lang = get_lang(request)
    auth_request = _email_test_auth_request(request)
    device_code = auth_request.get("device_code", "")
    try:
        polled = poll_warm_auth(device_code)
        if _email_test_auth_is_authorized(polled):
            _warm_store_auth(request, polled, device_code)
            flash(request, "success", t(lang, "email_test_authorized"))
        else:
            flash(request, "info", t(lang, "email_test_auth_pending"))
    except WarmApiError as exc:
        flash(request, "error", t(lang, "email_test_error", error=str(exc)))
    return redirect(EMAIL_TEST_SECTION)


@app.post("/email-test/reset")
async def email_test_reset(request: Request):
    _email_test_cache_delete(EMAIL_TEST_REPORT_CACHE, request.session.get("email_test_report_id", ""))
    pending = request.session.get("email_test_analysis_job") or {}
    _email_test_cache_delete(EMAIL_TEST_LOCAL_REPORT_CACHE, pending.get("job_id", ""))
    _clear_warm_auth(request)
    request.session["email_test_result"] = {}
    request.session["email_test_results"] = []
    request.session["email_test_report"] = {}
    request.session["email_test_report_id"] = ""
    request.session["email_test_analysis_job"] = {}
    request.session["email_test_analysis_error"] = ""
    return redirect(EMAIL_TEST_SECTION)


@app.post("/email-test/analyze")
async def email_test_analyze(
    request: Request,
    subject: str = Form(""),
    html_body: str = Form(""),
    unsubscribe_copy: str = Form(DEFAULT_UNSUBSCRIBE_COPY),
    signature: str = Form(DEFAULT_SIGNATURE),
):
    lang = get_lang(request)
    subject = normalize_subject_template(subject)
    unsubscribe_copy = normalize_unsubscribe_copy(unsubscribe_copy)
    request.session["draft_subject"] = subject
    request.session["draft_body"] = html_body
    request.session["draft_unsubscribe"] = unsubscribe_copy
    request.session["draft_signature"] = signature
    auth_data = _email_test_auth(request)
    if not auth_data.get("access_token"):
        flash(request, "error", t(lang, "email_test_no_auth"))
        return redirect(EMAIL_TEST_SECTION)

    sender_rows = sender_rows_one_per_domain([
        row
        for row in list_senders(include_credentials=False)
        if row.get("status") == "active" and normalize_email(row.get("email", ""))
    ])
    if not sender_rows:
        flash(request, "error", t(lang, "email_test_no_sender"))
        return redirect(EMAIL_TEST_SECTION)

    final_body = compose_email_template(html_body, unsubscribe_copy, signature)
    final_html = body_to_html(final_body)
    plain_text = html_to_plain_text(final_html)
    local_reports = []
    checks = []
    for sender in sender_rows:
        sender_email = normalize_email(sender.get("email", ""))
        sender_domain = get_domain(sender_email)
        local_report = analyze_email_locally(
            subject,
            final_html,
            plain_text=plain_text,
            sender_email=sender_email,
            ps_auto_added=True,
        )
        local_report["sender_domain"] = sender_domain
        local_reports.append(local_report)
        checks.append(
            {
                "sender_email": sender_email,
                "sender_domain": sender_domain,
                "subject": subject,
                "html_body": final_html,
                "plain_text": plain_text,
                "local_report": local_report,
            }
        )

    try:
        email_test_logger.info(
            "submit sender score analysis senders=%s domains=%s subject_len=%s body_len=%s",
            len(sender_rows),
            ",".join(sorted({item.get("sender_domain", "") for item in checks if item.get("sender_domain")})),
            len(subject or ""),
            len(final_body or ""),
        )
        job_data = analyze_email_deliverability(
            auth_data["access_token"],
            {
                "checks": checks,
                "client_generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        if not isinstance(job_data, dict):
            job_data = {"raw": job_data}
        email_test_logger.info("sender score bff response keys=%s status=%s", sorted(job_data.keys()), job_data.get("status", ""))
        backend_result = job_data.get("result") if isinstance(job_data.get("result"), dict) else {}
        if job_data.get("reports") or job_data.get("results"):
            report_id = f"etr_{uuid.uuid4()}"
            _email_test_cache_set(EMAIL_TEST_REPORT_CACHE, report_id, _combine_email_test_reports(local_reports, job_data))
            request.session["email_test_report_id"] = report_id
            request.session["email_test_report"] = {}
            request.session["email_test_analysis_job"] = {}
            request.session["email_test_analysis_error"] = ""
            flash(request, "success", t(lang, "email_test_sent", count=len(local_reports)))
        elif backend_result.get("reports") or backend_result.get("results"):
            report_id = f"etr_{uuid.uuid4()}"
            _email_test_cache_set(EMAIL_TEST_REPORT_CACHE, report_id, _combine_email_test_reports(local_reports, backend_result))
            request.session["email_test_report_id"] = report_id
            request.session["email_test_report"] = {}
            request.session["email_test_analysis_job"] = {}
            request.session["email_test_analysis_error"] = ""
            flash(request, "success", t(lang, "email_test_sent", count=len(local_reports)))
        elif job_data.get("job_id"):
            job_id = job_data.get("job_id", "")
            _email_test_cache_set(EMAIL_TEST_LOCAL_REPORT_CACHE, job_id, local_reports)
            request.session["email_test_analysis_job"] = {
                "job_id": job_id,
                "status": job_data.get("status", "queued"),
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            request.session["email_test_report"] = {}
            request.session["email_test_report_id"] = ""
            request.session["email_test_analysis_error"] = ""
            email_test_logger.info("sender score queued job_id=%s local_reports=%s", job_id, len(local_reports))
            flash(request, "success", t(lang, "email_test_analysis_queued"))
        else:
            email_test_logger.warning("sender score bff response missing job_id/reports payload=%s", job_data)
            request.session["email_test_analysis_job"] = {}
            request.session["email_test_report"] = {}
            request.session["email_test_report_id"] = ""
            request.session["email_test_analysis_error"] = "ePetrel returned no job id or reports. Please try again."
            flash(request, "error", t(lang, "email_test_backend_error", error="ePetrel returned no job id or reports. Please try again."))
        request.session["email_test_results"] = []
        request.session["email_test_result"] = {}
        request.session["email_test_diagnostics"] = {}
    except EmailTestApiError as exc:
        email_test_logger.exception("sender score analysis failed: %s", exc)
        request.session["email_test_analysis_job"] = {}
        request.session["email_test_report"] = {}
        request.session["email_test_report_id"] = ""
        request.session["email_test_analysis_error"] = str(exc)
        flash(request, "error", t(lang, "email_test_backend_error", error=str(exc)))
    return redirect(EMAIL_TEST_SECTION)


@app.post("/email-test/analyze/poll")
async def email_test_analyze_poll(request: Request):
    lang = get_lang(request)
    auth_data = _email_test_auth(request)
    pending = request.session.get("email_test_analysis_job") or {}
    job_id = pending.get("job_id", "")
    if not auth_data.get("access_token") or not job_id:
        return redirect(EMAIL_TEST_SECTION)

    try:
        job_data = poll_email_deliverability_analysis(auth_data["access_token"], job_id)
        if not isinstance(job_data, dict):
            job_data = {"raw": job_data}
        status = str(job_data.get("status") or "queued").lower()
        email_test_logger.info("sender score poll job_id=%s status=%s keys=%s", job_id, status, sorted(job_data.keys()))
        pending["status"] = status
        request.session["email_test_analysis_job"] = pending
        if status == "completed":
            backend_result = job_data.get("result") if isinstance(job_data.get("result"), dict) else job_data
            local_reports = _email_test_cache_get(EMAIL_TEST_LOCAL_REPORT_CACHE, job_id, [])
            report_id = f"etr_{uuid.uuid4()}"
            _email_test_cache_set(
                EMAIL_TEST_REPORT_CACHE,
                report_id,
                _combine_email_test_reports(local_reports, backend_result or {}),
            )
            request.session["email_test_report"] = {}
            request.session["email_test_report_id"] = report_id
            request.session["email_test_analysis_job"] = {}
            request.session["email_test_analysis_error"] = ""
            _email_test_cache_delete(EMAIL_TEST_LOCAL_REPORT_CACHE, job_id)
            flash(request, "success", t(lang, "email_test_sent", count=len(local_reports)))
        elif status == "failed":
            request.session["email_test_analysis_job"] = {}
            request.session["email_test_analysis_error"] = job_data.get("error") or "unknown"
            flash(request, "error", t(lang, "email_test_backend_error", error=job_data.get("error") or "unknown"))
    except EmailTestApiError as exc:
        flash(request, "error", t(lang, "email_test_backend_error", error=str(exc)))
        email_test_logger.exception("sender score poll failed: %s", exc)
    return redirect(EMAIL_TEST_SECTION)


# Deprecated Gmail seed placement route. Disabled in favor of /email-test/analyze.
# @app.post("/email-test/send")
async def email_test_send(
    request: Request,
    subject_prefix: str = Form("ePetrel Gmail placement test"),
    wait_for_result: str = Form(""),
):
    lang = get_lang(request)
    auth_data = _email_test_auth(request)
    if not auth_data.get("access_token"):
        flash(request, "error", t(lang, "email_test_no_auth"))
        return redirect(EMAIL_TEST_SECTION)

    sender_rows = sender_rows_one_per_domain([
        row
        for row in list_senders(include_credentials=True)
        if row.get("status") == "active" and normalize_email(row.get("email", ""))
    ])
    if not sender_rows:
        flash(request, "error", t(lang, "email_test_no_sender"))
        return redirect(EMAIL_TEST_SECTION)

    results = []
    sent_count = 0
    for sender in sender_rows:
        sender_email = sender["email"]
        sender_domain = get_domain(sender_email)
        allowed, used = can_run_email_test_for_domain(sender_domain, daily_limit=3)
        if not allowed:
            results.append(
                {
                    "sender_email": sender_email,
                    "sender_domain": sender_domain,
                    "status": "failed",
                    "error": t(lang, "email_test_domain_limited", domain=sender_domain, used=used),
                }
            )
            continue
        try:
            request_data = create_email_test_request(auth_data["access_token"], sender_email)
            request_id = request_data.get("emailtestrequestid") or request_data.get("request_id") or request_data.get("id") or ""
            target_gmail = email_test_gmail_from_auth(auth_data, request_data)
            if not request_id or not normalize_email(target_gmail):
                raise EmailTestApiError("ePetrel did not return a placement request id or target mailbox.")

            final_subject = f"{subject_prefix} [{request_id}]"
            final_html = (
                "<p>This is an ePetrel managed Gmail placement test.</p>"
                f"<p>emailtestrequestid: <strong>{request_id}</strong></p>"
                f"<p>Sender under test: {sender_email}</p>"
            )
            send_result = send_cold_email(
                sender_email,
                sender["password"],
                target_gmail,
                final_subject,
                final_html,
                html_to_plain_text(final_html),
                "email-placement-test",
                extra_headers={
                    "X-ePetrel-EmailTestRequestId": request_id,
                    "X-ePetrel-Test-Sender": sender_email,
                },
            )
            if send_result["status"] != "success":
                raise EmailTestApiError(send_result.get("error") or send_result["status"])

            sent_count += 1
            increment_email_test_domain_count(sender_domain)
            results.append(
                {
                    "sender_email": sender_email,
                    "sender_domain": sender_domain,
                    "request_id": request_id,
                    "emailtestrequestid": request_id,
                    "status": "sent",
                    "target_email": target_gmail,
                }
            )
        except EmailTestApiError as exc:
            results.append({"sender_email": sender_email, "sender_domain": sender_domain, "status": "failed", "error": str(exc)})

    if sent_count:
        if wait_for_result:
            results = refresh_email_test_results(auth_data["access_token"], results, wait=True)
        flash(request, "success", t(lang, "email_test_sent", count=sent_count))
    else:
        flash(request, "error", t(lang, "email_test_error", error="No test message was sent."))
    request.session["email_test_results"] = results
    request.session["email_test_result"] = results[0] if len(results) == 1 else {}
    request.session["email_test_auto_poll_count"] = 0
    request.session["email_test_auto_poll_pause_until"] = 0
    request.session["email_test_diagnostics"] = {}
    return redirect(EMAIL_TEST_SECTION)


# Deprecated Gmail API diagnostics route. Disabled with the old Gmail placement flow.
# @app.post("/email-test/diagnose")
async def email_test_diagnose(request: Request):
    lang = get_lang(request)
    auth_data = _email_test_auth(request)
    if not auth_data.get("access_token"):
        flash(request, "error", t(lang, "email_test_no_auth"))
        return redirect(EMAIL_TEST_SECTION)
    request.session["email_test_auto_poll_pause_until"] = time.time() + 60
    try:
        diagnostics = diagnose_email_test_gmail(auth_data["access_token"], run_scan=True)
        diagnostics["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        request.session["email_test_diagnostics"] = diagnostics
    except EmailTestApiError as exc:
        request.session["email_test_diagnostics"] = {
            "status": "failed",
            "error": str(exc),
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        flash(request, "error", t(lang, "email_test_diagnostics_fail", error=str(exc)))
    return redirect(EMAIL_TEST_SECTION)


# Deprecated Gmail placement polling route. Disabled with the old Gmail placement flow.
# @app.post("/email-test/poll")
async def email_test_poll(request: Request, request_id: str = Form("")):
    lang = get_lang(request)
    auth_data = _email_test_auth(request)
    try:
        current_results = request.session.get("email_test_results") or request.session.get("email_test_result") or []
        if request_id:
            current_results = [result for result in (current_results if isinstance(current_results, list) else [current_results]) if (result.get("request_id") or result.get("emailtestrequestid")) == request_id]
        refreshed = refresh_email_test_results(auth_data["access_token"], current_results, wait=False)
        request.session["email_test_results"] = refreshed
        request.session["email_test_result"] = refreshed[0] if len(refreshed) == 1 else {}
        if any(str(item.get("status") or "").lower() not in {"completed", "failed", "expired"} for item in refreshed):
            request.session["email_test_auto_poll_count"] = int(request.session.get("email_test_auto_poll_count") or 0) + 1
        else:
            request.session["email_test_auto_poll_count"] = 0
    except (EmailTestApiError, KeyError) as exc:
        flash(request, "error", t(lang, "email_test_error", error=str(exc)))
    return redirect(EMAIL_TEST_SECTION)


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
        if _email_test_auth_is_authorized(auth_data):
            if wants_json:
                return JSONResponse({"status": "authorized", "device_code": ""})
            flash(request, "success", "Logged in to ePetrel.")
            return redirect("/warm")
        auth_request = start_warm_auth()
        request.session["warm_auth_request"] = auth_request
        request.session["warm_auth_started_at"] = time.time()
        if _email_test_auth_is_authorized(auth_request):
            _warm_store_auth(request, auth_request, auth_request.get("device_code", ""))
        if wants_json:
            if _email_test_auth_is_authorized(auth_request):
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
    if _email_test_auth_is_authorized(auth_data):
        return JSONResponse({"status": "authorized"})

    auth_request = request.session.get("warm_auth_request") or {}
    device_code = request.query_params.get("device_code") or auth_request.get("device_code", "")
    if not device_code:
        return JSONResponse({"status": "not_started"})

    cached_auth = _email_test_cache_get(EMAIL_TEST_AUTH_CACHE, device_code)
    if _email_test_auth_is_authorized(cached_auth):
        _warm_store_auth(request, cached_auth, device_code)
        return JSONResponse({"status": "authorized"})

    try:
        auth = poll_warm_auth(device_code)
        if _email_test_auth_is_authorized(auth):
            _warm_store_auth(request, auth, device_code)
            return JSONResponse({"status": "authorized"})
        return JSONResponse({"status": "pending"})
    except WarmApiError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=502)


@app.post("/warm/auth/check")
async def warm_auth_check(request: Request):
    if _email_test_auth_is_authorized(_session_epetrel_auth(request, "warm_auth")):
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
        if placement in {"inbox", "spam", "other"} or status in {"found", "needs_imap", "error", "missing_sender"}:
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
    if capability_status == "missing_gmail_modify_scope":
        return {
            **sender,
            "warm_status": "manual_rescue_ready",
            "warm_status_label": "Manual rescue if Spam",
            "warm_status_message": capability.get("message") or "Automatic Spam-to-Inbox rescue is off.",
            "warm_selectable": True,
            "warm_mailbox": mailbox,
            "warm_capability": capability,
        }
    status = "imap_or_move_unavailable"
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
    elif result.get("status") == "needs_imap":
        flash(request, "warning", f"Reconnect Gmail API, or enable IMAP access for manual scanning. {GMAIL_MODIFY_SETUP_HINT}")
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


@app.get("/security", response_class=HTMLResponse)
async def security_page(request: Request, days: int = 7):
    days = max(1, min(int(days), 90))
    outbound_rows = query_rows(
        """
        SELECT id, timestamp, sender, receiver, target_domain, subject, variant_version, status, error, message_id
        FROM outbound_logs
        WHERE datetime(timestamp) >= datetime('now', ?)
        ORDER BY timestamp DESC
        """,
        (f"-{days} days",),
    )
    event_rows = query_rows(
        """
        SELECT id, event_time, sender, receiver, event_type, source, subject, message_id, target_domain, severity, details
        FROM delivery_events
        WHERE datetime(event_time) >= datetime('now', ?)
        ORDER BY event_time DESC
        """,
        (f"-{days} days",),
    )
    senders = list_senders()

    total_sent = sum(1 for row in outbound_rows if row.get("status") == "success")
    total_failed = sum(1 for row in outbound_rows if row.get("status") == "failed")
    hard_bounces = sum(1 for row in event_rows if row.get("event_type") == "bounced_hard")
    soft_bounces = sum(1 for row in event_rows if row.get("event_type") == "bounced_soft")
    unsubscribes = sum(1 for row in event_rows if row.get("event_type") == "unsubscribe")
    seed_inbox = sum(1 for row in event_rows if row.get("event_type") == "seed_inbox")
    seed_spam = sum(1 for row in event_rows if row.get("event_type") == "seed_spam")
    seed_missing = sum(1 for row in event_rows if row.get("event_type") == "seed_missing")
    total_bounces = hard_bounces + soft_bounces
    seed_found = seed_inbox + seed_spam

    metrics = [
        {"label": t(get_lang(request), "metric_sent"), "value": total_sent, "sub": ""},
        {"label": t(get_lang(request), "metric_failed"), "value": total_failed, "sub": ""},
        {"label": t(get_lang(request), "metric_bounce"), "value": f"{(total_bounces / total_sent if total_sent else 0):.2%}", "sub": str(total_bounces)},
        {"label": t(get_lang(request), "metric_hard"), "value": f"{(hard_bounces / total_sent if total_sent else 0):.2%}", "sub": str(hard_bounces)},
        {"label": t(get_lang(request), "metric_unsub"), "value": f"{(unsubscribes / total_sent if total_sent else 0):.2%}", "sub": str(unsubscribes)},
        {"label": t(get_lang(request), "metric_spam"), "value": f"{(seed_spam / seed_found if seed_found else 0):.2%}", "sub": f"{seed_spam}/{seed_found or 0}"},
    ]
    alerts = []
    if total_sent and total_bounces / total_sent > BOUNCE_RATE_ALERT:
        alerts.append({"level": "error", "message": f"Bounce rate is above {BOUNCE_RATE_ALERT:.2%}."})
    if total_sent and hard_bounces / total_sent > HARD_BOUNCE_RATE_ALERT:
        alerts.append({"level": "error", "message": f"Hard bounce rate is above {HARD_BOUNCE_RATE_ALERT:.2%}."})
    if total_sent and unsubscribes / total_sent > UNSUBSCRIBE_RATE_ALERT:
        alerts.append({"level": "warning", "message": f"Unsubscribe rate is above {UNSUBSCRIBE_RATE_ALERT:.2%}."})
    if seed_found and seed_spam / seed_found > SPAM_PLACEMENT_RATE_ALERT:
        alerts.append({"level": "error", "message": f"Seed spam placement is above {SPAM_PLACEMENT_RATE_ALERT:.2%}."})
    if seed_missing:
        alerts.append({"level": "warning", "message": f"{seed_missing} seed emails were not found in monitored folders."})

    return templates.TemplateResponse(
        request=request,
        name="security.html",
        context=page_context(
            request,
            "security",
            "security_title",
            "security_caption",
            days=days,
            metrics=metrics,
            alerts=alerts,
            seeds=list_seed_accounts(),
            senders=senders,
            events=event_rows[:100],
            outbound=outbound_rows[:100],
        ),
    )


@app.post("/seeds")
async def save_seed(
    request: Request,
    seed_email: str = Form(""),
    seed_password: str = Form(""),
    provider: str = Form("Gmail"),
    imap_host: str = Form("imap.gmail.com"),
    imap_port: int = Form(993),
    inbox_folder: str = Form("INBOX"),
    spam_folder: str = Form("Spam"),
    status: str = Form("active"),
):
    lang = get_lang(request)
    normalized = normalize_email(seed_email)
    if not normalized or not seed_password or not imap_host:
        flash(request, "error", t(lang, "valid_seed_error"))
    else:
        upsert_seed_account(normalized, seed_password, provider, imap_host, imap_port, inbox_folder, spam_folder, status)
        flash(request, "success", t(lang, "saved_seed", email=normalized))
    return redirect("/security")


@app.post("/security/sync-seeds")
async def sync_seeds(request: Request, seed_limit: int = Form(80), days: int = Form(7)):
    lang = get_lang(request)
    results = check_all_seed_accounts(limit_per_folder=int(seed_limit))
    if not results:
        flash(request, "warning", t(lang, "no_active_seed"))
    for result in results:
        if result["error"]:
            flash(request, "warning", f"{result['seed']}: {result['error']}")
        else:
            flash(request, "success", t(lang, "seed_sync_success", seed=result["seed"], matched=result["matched"], missing=result["missing"]))
    return redirect(f"/security?days={int(days)}")


@app.post("/seeds/clear")
async def clear_seeds_route(request: Request):
    lang = get_lang(request)
    deleted = clear_seed_accounts()
    flash(request, "success", t(lang, "cleared_seeds", count=deleted))
    return redirect("/security")


@app.post("/security/outbound/clear")
async def clear_security_outbound_route(request: Request):
    lang = get_lang(request)
    deleted = clear_outbound_logs()
    flash(request, "success", t(lang, "cleared_security_outbound", count=deleted))
    return redirect("/security")


@app.post("/security/events/clear")
async def clear_security_events_route(request: Request):
    lang = get_lang(request)
    deleted = clear_delivery_events()
    flash(request, "success", t(lang, "cleared_security_events", count=deleted))
    return redirect("/security")


def crm_filters(status="", q="", tags="", due="", external_touch_status="", campaign=""):
    return {
        "status": (status or "").strip().lower(),
        "q": (q or "").strip(),
        "tags": split_tags(tags),
        "due": (due or "").strip().lower(),
        "external_touch_status": (external_touch_status or "").strip().lower(),
        "campaign": (campaign or "").strip(),
    }


def crm_query_string(filters, page=None):
    query = {}
    for key in ["status", "q", "due", "external_touch_status", "campaign"]:
        value = filters.get(key)
        if value:
            query[key] = value
    if filters.get("tags"):
        query["tags"] = ",".join(filters["tags"])
    if page is not None:
        query["page"] = page
    encoded = urlencode(query)
    return f"?{encoded}" if encoded else ""


def crm_contacts_csv_response(rows, filename_prefix="epetrel-crm-contacts"):
    export_rows = []
    for row in rows:
        item = dict(row)
        item["tags"] = ", ".join(row.get("tags") or [])
        item.pop("custom_fields", None)
        item["custom_fields_json"] = json.dumps(row.get("custom_fields") or {}, ensure_ascii=False)
        export_rows.append(item)
    output = BytesIO()
    pd.DataFrame(export_rows).to_csv(output, index=False, encoding="utf-8-sig")
    output.seek(0)
    filename = f"{filename_prefix}-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(
        output,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/crm", response_class=HTMLResponse)
async def crm_page(
    request: Request,
    page: int = 1,
    status: str = "",
    q: str = "",
    tags: str = "",
    due: str = "",
    external_touch_status: str = "",
    campaign: str = "",
):
    abandon_due_crm_contacts()
    filters = crm_filters(status, q, tags, due, external_touch_status, campaign)
    total_rows = list_crm_contacts(filters, limit=1, offset=0)[1]
    pages = max(1, (total_rows + CRM_PAGE_SIZE - 1) // CRM_PAGE_SIZE)
    page = max(1, min(int(page or 1), pages))
    contacts, total = list_crm_contacts(filters, limit=CRM_PAGE_SIZE, offset=(page - 1) * CRM_PAGE_SIZE)
    return templates.TemplateResponse(
        request=request,
        name="crm.html",
        context=page_context(
            request,
            "crm",
            "crm_title",
            "crm_caption",
            contacts=contacts,
            crm_filters=filters,
            crm_statuses=crm_status_options(),
            crm_tags=list_crm_tags(),
            crm_summary=crm_dashboard_summary(),
            crm_funnel=crm_funnel_stats(),
            crm_tasks_today=list_crm_tasks(status="open", due="today"),
            remarketing_templates=list_remarketing_templates(),
            remarketing_candidates=len(list_remarketing_candidates(limit=1000)),
            crm_max_remarketing_attempts=get_crm_max_remarketing_attempts(),
            obsidian_export_path=get_app_setting(CRM_OBSIDIAN_PATH_SETTING_KEY, str(CRM_DEFAULT_OBSIDIAN_DIR)),
            crm_page={
                "page": page,
                "pages": pages,
                "total": total,
                "prev_url": f"/crm{crm_query_string(filters, page - 1)}" if page > 1 else "",
                "next_url": f"/crm{crm_query_string(filters, page + 1)}" if page < pages else "",
            },
        ),
    )


@app.get("/crm/export")
async def crm_export(
    status: str = "",
    q: str = "",
    tags: str = "",
    due: str = "",
    external_touch_status: str = "",
    campaign: str = "",
):
    filters = crm_filters(status, q, tags, due, external_touch_status, campaign)
    return crm_contacts_csv_response(list_crm_contacts_for_export(filters))


@app.post("/crm/import")
async def crm_import_contacts(request: Request, contacts_file: UploadFile = File(None)):
    lang = get_lang(request)
    df = await load_lead_dataframe(contacts_file, allow_sample=False)
    if df is None or "Email" not in df.columns:
        flash(request, "error", t(lang, "missing_email_col"))
        return redirect("/crm")
    imported = upsert_crm_contacts_from_records(records_from_df(df), max_attempts=get_crm_max_remarketing_attempts())
    flash(request, "success", t(lang, "crm_import_done", count=imported))
    return redirect("/crm")


@app.get("/crm/contacts/{email:path}", response_class=HTMLResponse)
async def crm_contact_detail(request: Request, email: str):
    contact = get_crm_contact(email)
    if not contact:
        flash(request, "warning", t(get_lang(request), "crm_contact_missing"))
        return redirect("/crm")
    return templates.TemplateResponse(
        request=request,
        name="crm_detail.html",
        context=page_context(
            request,
            "crm",
            "crm_title",
            "crm_caption",
            contact=contact,
            timeline=get_crm_timeline(contact["email"]),
            tasks=list_crm_tasks(contact_email=contact["email"], status=""),
            crm_statuses=crm_status_options(),
            crm_tags=list_crm_tags(),
        ),
    )


@app.post("/crm/settings")
async def save_crm_settings(
    request: Request,
    max_remarketing_attempts: int = Form(CRM_DEFAULT_REMARKETING_MAX),
    obsidian_export_path: str = Form(""),
):
    set_crm_max_remarketing_attempts(max_remarketing_attempts)
    upsert_app_setting(CRM_OBSIDIAN_PATH_SETTING_KEY, obsidian_export_path.strip() or str(CRM_DEFAULT_OBSIDIAN_DIR))
    flash(request, "success", t(get_lang(request), "crm_saved"))
    return redirect("/crm")


@app.post("/crm/contacts/status")
async def crm_update_status(
    request: Request,
    email: str = Form(""),
    status: str = Form("pending"),
    note: str = Form(""),
    next_url: str = Form("/crm"),
):
    set_crm_contact_status(email, status, note=note, actor="manual")
    flash(request, "success", t(get_lang(request), "crm_saved"))
    return redirect(local_redirect_target(next_url, "/crm"))


@app.post("/crm/contacts/notes")
async def crm_update_notes(
    request: Request,
    email: str = Form(""),
    note: str = Form(""),
    next_url: str = Form("/crm"),
):
    append_crm_note(email, note, actor="manual")
    flash(request, "success", t(get_lang(request), "crm_saved"))
    return redirect(local_redirect_target(next_url, "/crm"))


@app.post("/crm/contacts/tags")
async def crm_update_tags(
    request: Request,
    email: str = Form(""),
    tags: str = Form(""),
    tag_action: str = Form("add"),
    next_url: str = Form("/crm"),
):
    if tag_action == "remove":
        remove_crm_tags(email, split_tags(tags))
    else:
        add_crm_tags(email, split_tags(tags))
    flash(request, "success", t(get_lang(request), "crm_saved"))
    return redirect(local_redirect_target(next_url, "/crm"))


@app.post("/crm/contacts/channels")
async def crm_update_channels(
    request: Request,
    email: str = Form(""),
    whatsapp: str = Form(""),
    instagram: str = Form(""),
    linkedin: str = Form(""),
    external_touch_status: str = Form("none"),
    external_touch_channel: str = Form(""),
    next_url: str = Form("/crm"),
):
    update_crm_channels(
        email,
        whatsapp=whatsapp,
        instagram=instagram,
        linkedin=linkedin,
        external_touch_status=external_touch_status,
        external_touch_channel=external_touch_channel,
        actor="manual",
    )
    flash(request, "success", t(get_lang(request), "crm_saved"))
    return redirect(local_redirect_target(next_url, "/crm"))


@app.post("/crm/bulk")
async def crm_bulk_action(request: Request):
    lang = get_lang(request)
    form = await request.form()
    emails = [normalize_email(value) for value in form.getlist("emails")]
    emails = [email for email in emails if email]
    action = str(form.get("bulk_action") or "").strip()
    next_url = local_redirect_target(str(form.get("next_url") or "/crm"), "/crm")
    if not emails:
        flash(request, "warning", t(lang, "crm_contact_missing"))
        return redirect(next_url)
    if action == "export":
        rows = [get_crm_contact(email) for email in emails]
        return crm_contacts_csv_response([row for row in rows if row], filename_prefix="epetrel-crm-selected")
    changed = 0
    for email in emails:
        if action == "status":
            set_crm_contact_status(email, str(form.get("bulk_status") or "pending"), note=str(form.get("bulk_note") or ""), actor="bulk")
            changed += 1
        elif action == "note":
            append_crm_note(email, str(form.get("bulk_note") or ""), actor="bulk")
            changed += 1
        elif action == "next_followup":
            set_crm_next_followup(email, str(form.get("bulk_next_followup_at") or ""), actor="bulk")
            changed += 1
        elif action == "tag_add":
            add_crm_tags(email, split_tags(str(form.get("bulk_tags") or "")))
            changed += 1
        elif action == "tag_remove":
            remove_crm_tags(email, split_tags(str(form.get("bulk_tags") or "")))
            changed += 1
        elif action == "external_add":
            mark_crm_external_touch(email, status="pending", channel=str(form.get("bulk_external_channel") or ""), note=str(form.get("bulk_note") or ""), actor="bulk")
            changed += 1
        elif action == "external_remove":
            mark_crm_external_touch(email, status="none", channel="", note=str(form.get("bulk_note") or ""), actor="bulk")
            changed += 1
    flash(request, "success", t(lang, "crm_bulk_done", count=changed))
    return redirect(next_url)


@app.post("/crm/tasks")
async def crm_save_task(
    request: Request,
    email: str = Form(""),
    task_type: str = Form("custom"),
    title: str = Form(""),
    due_at: str = Form(""),
    notes: str = Form(""),
    channel: str = Form(""),
    task_id: int = Form(0),
    status: str = Form("open"),
    next_url: str = Form("/crm"),
):
    upsert_crm_task(email, task_type=task_type, title=title, due_at=due_at, notes=notes, channel=channel, task_id=task_id, status=status)
    flash(request, "success", t(get_lang(request), "crm_saved"))
    return redirect(local_redirect_target(next_url, "/crm"))


@app.post("/crm/remarketing/templates/save")
async def crm_save_remarketing_template(
    request: Request,
    step_number: int = Form(1),
    name: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    unsubscribe_copy: str = Form(""),
    signature: str = Form(""),
    cooldown_days: int = Form(7),
    status: str = Form("active"),
):
    lang = get_lang(request)
    step = max(1, min(CRM_HARD_REMARKETING_MAX, int(step_number or 1)))
    if (
        not validate_spintax_format(subject)
        or not validate_spintax_format(body)
        or not validate_spintax_format(unsubscribe_copy)
        or not validate_spintax_format(signature)
    ):
        flash(request, "error", t(lang, "variant_format_error"))
        return redirect("/crm#remarketing")
    upsert_remarketing_template(step, name, subject, body, unsubscribe_copy, signature, cooldown_days, status)
    flash(request, "success", t(lang, "crm_template_saved", step=step))
    return redirect("/crm#remarketing")


@app.post("/crm/remarketing/send")
async def crm_send_remarketing(
    request: Request,
    limit: int = Form(25),
    delay_min: int = Form(60),
    delay_max: int = Form(180),
):
    lang = get_lang(request)
    candidates = list_remarketing_candidates(limit=max(1, min(int(limit or 25), 250)))
    sent = skipped = failed = 0
    delay_min, delay_max = min(delay_min, delay_max), max(delay_min, delay_max)
    sender_sequences = {}
    for idx, contact in enumerate(candidates):
        target_email = normalize_email(contact.get("email", ""))
        if not target_email or crm_contact_auto_excluded(target_email):
            skipped += 1
            continue
        step = int(contact.get("remarketing_attempts") or 0) + 1
        template = get_remarketing_template(step)
        if not template or template.get("status") == "paused" or not template.get("subject") or not template.get("body"):
            skipped += 1
            log_crm_activity(target_email, "remarketing_skipped", f"Missing active template for step {step}", status="skipped", actor="crm")
            continue
        sender_pool = get_active_senders(get_domain(target_email))
        if not sender_pool:
            skipped += 1
            log_crm_activity(target_email, "remarketing_skipped", "No healthy sender is available", status="skipped", actor="crm")
            continue
        current_sender, current_pwd = sender_pool[0]
        sender_sequences[current_sender] = sender_sequences.get(current_sender, 0) + 1
        sequence_no = sender_sequences[current_sender]
        record = crm_contact_record_for_template(contact)
        record["Sender_Name"] = current_sender
        company = contact.get("company") or "your team"
        icebreaker = f"I hope you and the team at {company} are doing well."
        full_body = compose_email_template(template.get("body"), template.get("unsubscribe_copy"), template.get("signature"))
        final_subject = render_variant_template(template.get("subject"), record, icebreaker, seed=f"{current_sender}:{step}:{sequence_no}:subject")
        final_body = render_variant_template(full_body, record, icebreaker, seed=f"{current_sender}:{step}:{sequence_no}:body")
        final_html = body_to_html(final_body)
        final_plain = html_to_plain_text(final_html)
        result = send_cold_email(
            current_sender,
            current_pwd,
            target_email,
            final_subject,
            final_html,
            final_plain,
            f"Remarketing-{step}",
            crm_remarketing_step=step,
            crm_template_name=template.get("name") or f"Remarketing {step}",
        )
        if result["status"] == "success":
            sent += 1
            mark_crm_outbound(
                target_email,
                outbound_log_id=result.get("log_id") or 0,
                sender=current_sender,
                subject=final_subject,
                body_html=final_html,
                variant_version=f"Remarketing-{step}",
                remarketing_step=step,
                template_name=template.get("name") or f"Remarketing {step}",
                next_followup_at=crm_next_followup_at(template.get("cooldown_days")),
            )
        elif result["status"] == "skipped":
            skipped += 1
        else:
            failed += 1
        if idx < len(candidates) - 1 and delay_max > 0:
            await dispatch_sleep(request, calculate_dispatch_delay(delay_min, delay_max, idx + 1))
    flash(request, "success", t(lang, "crm_remarketing_done", sent=sent, skipped=skipped, failed=failed))
    return redirect("/crm#remarketing")


@app.post("/crm/obsidian/export")
async def crm_obsidian_export(request: Request, obsidian_export_path: str = Form("")):
    if obsidian_export_path.strip():
        upsert_app_setting(CRM_OBSIDIAN_PATH_SETTING_KEY, obsidian_export_path.strip())
    result = export_crm_obsidian_notes(obsidian_export_path.strip())
    flash(request, "success", t(get_lang(request), "crm_obsidian_exported", count=result["count"], path=result["path"]))
    return redirect("/crm#obsidian")


@app.get("/api/crm/contacts")
async def api_crm_contacts(
    status: str = "",
    q: str = "",
    tags: str = "",
    due: str = "",
    external_touch_status: str = "",
    campaign: str = "",
    limit: int = 100,
    offset: int = 0,
):
    filters = crm_filters(status, q, tags, due, external_touch_status, campaign)
    rows, total = list_crm_contacts(filters, limit=max(1, min(int(limit or 100), 1000)), offset=max(0, int(offset or 0)))
    return JSONResponse({"contacts": rows, "total": total})


@app.get("/api/crm/external-touch-queue")
async def api_crm_external_touch_queue(channel: str = "", limit: int = 100):
    return JSONResponse({"contacts": list_external_touch_queue(channel=channel, limit=max(1, min(int(limit or 100), 1000)))})


@app.get("/api/crm/contacts/{email:path}")
async def api_crm_contact(email: str):
    contact = get_crm_contact(email)
    if not contact:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"contact": contact, "timeline": get_crm_timeline(contact["email"]), "tasks": list_crm_tasks(contact_email=contact["email"], status="")})


async def api_json_payload(request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


@app.post("/api/crm/contacts/{email:path}/channels")
async def api_crm_update_channels(request: Request, email: str):
    payload = await api_json_payload(request)
    update_crm_channels(
        email,
        whatsapp=payload.get("whatsapp"),
        instagram=payload.get("instagram"),
        linkedin=payload.get("linkedin"),
        external_touch_status=payload.get("external_touch_status"),
        external_touch_channel=payload.get("external_touch_channel"),
        enrichment_status=payload.get("enrichment_status"),
        actor="api",
    )
    return JSONResponse({"status": "saved", "contact": get_crm_contact(email)})


@app.post("/api/crm/contacts/{email:path}/external-touch")
async def api_crm_external_touch(request: Request, email: str):
    payload = await api_json_payload(request)
    mark_crm_external_touch(
        email,
        status=payload.get("status") or payload.get("external_touch_status") or "pending",
        channel=payload.get("channel") or payload.get("external_touch_channel") or "",
        note=payload.get("note") or "",
        actor="api",
    )
    return JSONResponse({"status": "saved", "contact": get_crm_contact(email)})


@app.post("/api/crm/contacts/{email:path}/status")
async def api_crm_status(request: Request, email: str):
    payload = await api_json_payload(request)
    set_crm_contact_status(email, payload.get("status") or "pending", note=payload.get("note") or "", actor="api")
    return JSONResponse({"status": "saved", "contact": get_crm_contact(email)})


@app.post("/api/crm/obsidian/export")
async def api_crm_obsidian_export(request: Request):
    payload = await api_json_payload(request)
    result = export_crm_obsidian_notes(payload.get("path") or "")
    return JSONResponse({"status": "exported", **result})


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    inspect_id: int = 0,
    page: int = 1,
    status: str = "",
    sender: str = "",
    receiver: str = "",
    domain: str = "",
    error_q: str = "",
):
    filters = audit_filter_context(status, sender, receiver, domain, error_q)
    where_sql, where_params = audit_where_clause(filters)
    total_rows = query_rows(f"SELECT COUNT(*) AS count FROM outbound_logs {where_sql}", where_params)
    total = int(total_rows[0]["count"] or 0) if total_rows else 0
    pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
    page = max(1, min(int(page or 1), pages))
    offset = (page - 1) * AUDIT_PAGE_SIZE
    logs = query_rows(
        f"""
        SELECT id, timestamp, sender, receiver, target_domain, subject, variant_version, status, error, message_id
        FROM outbound_logs
        {where_sql}
        ORDER BY datetime(timestamp) DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (*where_params, AUDIT_PAGE_SIZE, offset),
    )
    raw_html = ""
    if inspect_id:
        rows = query_rows("SELECT body_html FROM outbound_logs WHERE id = ?", (inspect_id,))
        raw_html = rows[0]["body_html"] if rows else ""
        if not rows:
            flash(request, "error", t(get_lang(request), "not_found"))
    return templates.TemplateResponse(
        request=request,
        name="audit.html",
        context=page_context(
            request,
            "audit",
            "audit_title",
            "audit_caption",
            logs=logs,
            inspect_id=inspect_id,
            raw_html=raw_html,
            audit_filters=filters,
            audit_export_url=f"/audit/export{audit_query_string(filters)}",
            audit_page={
                "page": page,
                "pages": pages,
                "total": total,
                "has_prev": page > 1,
                "has_next": page < pages,
                "prev_url": f"/audit{audit_query_string(filters, page - 1)}" if page > 1 else "",
                "next_url": f"/audit{audit_query_string(filters, page + 1)}" if page < pages else "",
                "page_label": t(get_lang(request), "report_page", page=page, pages=pages),
            },
        ),
    )


@app.get("/audit/export")
async def export_unsent_audit_receivers(
    request: Request,
    status: str = "",
    sender: str = "",
    receiver: str = "",
    domain: str = "",
    error_q: str = "",
):
    lang = get_lang(request)
    filters = audit_filter_context(status, sender, receiver, domain, error_q)
    where_sql, where_params = audit_where_clause(filters, export_unsent=True)
    rows = query_rows(
        f"""
        SELECT
            LOWER(receiver) AS receiver,
            MAX(timestamp) AS last_seen_at,
            COUNT(*) AS attempts,
            GROUP_CONCAT(DISTINCT status) AS statuses,
            GROUP_CONCAT(DISTINCT sender) AS senders,
            GROUP_CONCAT(DISTINCT target_domain) AS domains,
            GROUP_CONCAT(DISTINCT error) AS reasons
        FROM outbound_logs
        {where_sql}
          {"AND" if where_sql else "WHERE"} COALESCE(receiver, '') != ''
        GROUP BY LOWER(receiver)
        ORDER BY datetime(last_seen_at) DESC, receiver ASC
        """,
        where_params,
    )
    if not rows:
        flash(request, "warning", t(lang, "audit_export_empty"))
        return redirect(f"/audit{audit_query_string(filters)}")

    output = BytesIO()
    df = pd.DataFrame(rows)
    df.to_csv(output, index=False, encoding="utf-8-sig")
    output.seek(0)
    filename = f"epetrel-unsent-receivers-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(
        output,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/audit/delete")
async def delete_audit_log(
    request: Request,
    log_id: int = Form(0),
    page: int = Form(1),
    status: str = Form(""),
    sender: str = Form(""),
    receiver: str = Form(""),
    domain: str = Form(""),
    error_q: str = Form(""),
):
    lang = get_lang(request)
    deleted = delete_outbound_log(log_id)
    if deleted:
        flash(request, "success", t(lang, "audit_deleted", id=int(log_id or 0)))
    else:
        flash(request, "error", t(lang, "audit_delete_missing"))
    filters = audit_filter_context(status, sender, receiver, domain, error_q)
    return redirect(f"/audit{audit_query_string(filters, max(1, int(page or 1)))}")


@app.post("/audit/clear")
async def clear_audit_logs(request: Request):
    lang = get_lang(request)
    deleted = clear_outbound_logs()
    flash(request, "success", t(lang, "audit_cleared", count=deleted))
    return redirect("/audit")


@app.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request):
    inbox = query_rows(
        """
        SELECT received_at, sender, receiver, subject, sentiment
        FROM inbound_emails
        ORDER BY received_at DESC
        LIMIT 250
        """
    )
    return templates.TemplateResponse(
        request=request,
        name="inbox.html",
        context=page_context(request, "inbox", "inbox_title", "inbox_caption", inbox=inbox),
    )


@app.post("/inbox/sync")
async def sync_inbox(request: Request, limit_per_sender: int = Form(25)):
    lang = get_lang(request)
    for result in fetch_all_inboxes(limit_per_sender=int(limit_per_sender)):
        if result["error"]:
            flash(request, "warning", f"{result['sender']}: {result['error']}")
        else:
            flash(request, "success", t(lang, "inbox_sync_success", sender=result["sender"], stored=result["stored"]))
    return redirect("/inbox")


@app.get("/llm", response_class=HTMLResponse)
async def llm_page(request: Request, provider: str = "openai", warm_provider: str = "openai"):
    return redirect("/config")


@app.post("/llm")
async def save_llm(
    request: Request,
    provider: str = Form("openai"),
    scope: str = Form("cold"),
    warm_provider: str = Form("openai"),
    cold_provider: str = Form("openai"),
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
    target_provider = "warm_openai"
    upsert_llm_settings(target_provider, api_key=api_key, base_url=base_url, model=model, system_prompt=system_prompt, status="active")
    flash(request, "success", t(lang, "llm_saved"))
    return redirect("/config")


async def config_page(request: Request):
    warm_provider_settings = get_llm_settings("warm_openai") or {}
    proxy_settings = get_proxy_settings()
    return templates.TemplateResponse(
        request=request,
        name="config.html",
        context=page_context(
            request,
            "config",
            "config_title",
            "config_caption",
            senders=list_senders(),
            mail_provider_rows=MAIL_PROVIDER_ROWS,
            gmail_oauth_authorization_url=request.session.pop("gmail_oauth_authorization_url", ""),
            gmail_consumer_daily_limit=GMAIL_ACCOUNT_TYPE_DAILY_LIMITS["consumer_gmail"],
            gmail_workspace_daily_limit=GMAIL_ACCOUNT_TYPE_DAILY_LIMITS["workspace_gmail"],
            available_sender_count=len(get_active_senders()),
            warm_provider_settings=warm_provider_settings,
            warm_default_base_url=OPENAI_BASE_URL,
            warm_default_model="gpt-4o-mini",
            warm_default_system_prompt=WARM_LLM_SYSTEM_PROMPT,
            proxy_settings=proxy_settings,
        ),
    )


async def warm_legacy_redirect(request: Request):
    query = request.url.query
    target = "/" + (f"?{query}" if query else "")
    return RedirectResponse(target, status_code=302)


def install_mutualwarm_routes():
    blocked_prefixes = (
        "/dispatch",
        "/leads",
        "/crm",
        "/audit",
        "/inbox",
        "/security",
        "/seeds",
        "/email-test",
        "/api/crm",
        "/settings/remarketing-cooldown",
        "/template-defaults",
        "/email-templates",
        "/variants",
    )
    next_routes = []
    for route in app.router.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if path == "/warm" and "GET" in methods:
            continue
        if any(path == prefix or path.startswith(prefix + "/") for prefix in blocked_prefixes):
            continue
        next_routes.append(route)
    app.router.routes = next_routes
    app.add_api_route("/warm", warm_legacy_redirect, methods=["GET"], response_class=RedirectResponse, include_in_schema=False)
    app.add_api_route("/config", config_page, methods=["GET"], response_class=HTMLResponse)


install_mutualwarm_routes()
