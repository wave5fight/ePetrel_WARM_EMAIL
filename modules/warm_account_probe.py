import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from database.db_manager import get_sender
from config import MAIL_FROM_NAME
from modules.gmail_api import (
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    find_gmail_message_placement,
    move_gmail_message_to_inbox,
    send_gmail_api_message,
)


TOKEN_RE = re.compile(r"ePetrel warm verification token:\s*([A-Za-z0-9_-]{16,128})", re.IGNORECASE)
GMAIL_MODIFY_SETUP_HINT = (
    "Open Google Cloud Console > OAuth consent screen > Data access, add "
    "https://www.googleapis.com/auth/gmail.readonly and https://www.googleapis.com/auth/gmail.modify, "
    "save the consent screen, then reconnect Gmail with Full Auto Warm scopes enabled. "
    "These scopes are required for automatic Warm ownership probing, scanning, inbox rescue, and replies."
)


def warm_inbox_rescue_capability(mailbox_email):
    sender = get_sender(mailbox_email)
    if not sender:
        return {
            "capable": False,
            "status": "missing_sender",
            "message": "Save this Gmail mailbox in the local sender pool before enabling Full Auto Warm.",
        }

    auth_method = sender.get("auth_method") or ""
    if auth_method == "gmail_api":
        scopes = set(str(sender.get("gmail_granted_scopes") or "").replace(",", " ").split())
        if not sender.get("gmail_refresh_token") or not sender.get("gmail_client_id") or not sender.get("gmail_client_secret"):
            return {
                "capable": False,
                "status": "gmail_reconnect_required",
                "message": "Reconnect Gmail API before running warm probes.",
            }
        if GMAIL_READONLY_SCOPE not in scopes or GMAIL_MODIFY_SCOPE not in scopes:
            return {
                "capable": False,
                "status": "missing_gmail_modify_scope",
                "message": "Reconnect Gmail API with Full Auto Warm Gmail read/rescue scopes enabled.",
            }
        return {"capable": True, "status": "gmail_modify_ready", "method": "gmail_api"}

    return {
        "capable": False,
        "status": "gmail_api_required",
        "message": "Connect this mailbox with Gmail API before enabling Full Auto Warm.",
    }


def _extract_verification_token(text):
    match = TOKEN_RE.search(text or "")
    return match.group(1) if match else ""


def scan_warm_account_probe(mailbox_email, token, subject=""):
    sender = get_sender(mailbox_email)
    if not sender:
        return {"placement": "missing", "status": "missing_sender", "error": "Save this Gmail sender locally before scanning."}

    if (sender.get("auth_method") or "") != "gmail_api" or not sender.get("gmail_refresh_token"):
        return {
            "placement": "",
            "status": "gmail_api_required",
            "error": "Connect this mailbox with Gmail API before scanning warm probes.",
            "scanner": "gmail_api",
            "retryable_scan": False,
        }

    scopes = set(str(sender.get("gmail_granted_scopes") or "").replace(",", " ").split())
    if GMAIL_READONLY_SCOPE not in scopes:
        return {
            "placement": "",
            "status": "missing_gmail_readonly_scope",
            "error": "Reconnect Gmail API with readonly/modify scopes before scanning warm probes.",
            "scanner": "gmail_api",
            "retryable_scan": False,
        }

    try:
        result = find_gmail_message_placement(
            sender.get("gmail_client_id") or "",
            sender.get("gmail_client_secret") or "",
            sender.get("gmail_refresh_token") or "",
            token,
        )
        return {
            "placement": result.get("placement", "missing"),
            "status": "found" if result.get("placement") != "missing" else "missing",
            "folder": ",".join(result.get("labels") or []),
            "message_id": result.get("message_id", ""),
            "rfc822_message_id": result.get("rfc822_message_id", ""),
            "thread_id": result.get("thread_id", ""),
            "from_email": result.get("from_email", ""),
            "references": result.get("references", ""),
            "subject": result.get("subject", ""),
            "verification_token": _extract_verification_token(f"{result.get('subject', '')}\n{result.get('body', '')}"),
            "scanner": "gmail_api",
        }
    except Exception as exc:
        return {
            "placement": "",
            "status": "gmail_api_unavailable",
            "error": str(exc),
            "scanner": "gmail_api",
            "retryable_scan": True,
        }


def move_warm_account_probe_to_inbox(mailbox_email, scan_result):
    sender = get_sender(mailbox_email)
    if not sender:
        return {"moved": False, "error": "missing_sender"}

    if (sender.get("auth_method") or "") == "gmail_api" and sender.get("gmail_refresh_token") and scan_result.get("message_id"):
        try:
            move_gmail_message_to_inbox(
                sender.get("gmail_client_id") or "",
                sender.get("gmail_client_secret") or "",
                sender.get("gmail_refresh_token") or "",
                scan_result.get("message_id") or "",
            )
            return {"moved": True, "method": "gmail_api"}
        except Exception as exc:
            return {"moved": False, "method": "gmail_api", "error": str(exc)}

    return {"moved": False, "method": "gmail_api", "error": "missing_gmail_api_message_context"}


def send_warm_account_probe_reply(mailbox_email, scan_result):
    sender = get_sender(mailbox_email)
    if not sender:
        return {"sent": False, "error": "missing_sender"}
    if scan_result.get("placement") != "inbox":
        return {"sent": False, "error": "reply_requires_inbox"}

    to_email = parseaddr(scan_result.get("from_email") or "")[1]
    if not to_email:
        return {"sent": False, "error": "missing_probe_sender"}

    original_subject = scan_result.get("subject") or "Your ePetrel warm account is ready"
    subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
    sender_email = sender["email"]
    sender_domain = sender_email.split("@", 1)[1] if "@" in sender_email else "localhost"
    from_name = sender.get("from_name") or MAIL_FROM_NAME
    original_message_id = scan_result.get("rfc822_message_id") or scan_result.get("message_id") or ""
    references = " ".join(item for item in [scan_result.get("references", ""), original_message_id] if item).strip()

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, sender_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_domain)
    if original_message_id:
        msg["In-Reply-To"] = original_message_id
    if references:
        msg["References"] = references
    msg["X-ePetrel-Warm-Ownership-Reply"] = "1"
    msg.set_content("Hi,\n\nConfirmed. This warm mailbox can receive ePetrel account email in the inbox.\n\nThanks")

    try:
        if (sender.get("auth_method") or "") != "gmail_api":
            return {"sent": False, "error": "gmail_api_required"}
        send_gmail_api_message(
            sender.get("gmail_client_id") or "",
            sender.get("gmail_client_secret") or "",
            sender.get("gmail_refresh_token") or "",
            msg.as_bytes(),
        )
        return {"sent": True, "message_id": msg["Message-ID"]}
    except Exception as exc:
        return {"sent": False, "error": str(exc)}
