import email
import imaplib
import re
import smtplib
from email.message import EmailMessage
from email.header import decode_header
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from database.db_manager import get_sender
from config import MAIL_FROM_NAME, MAILFORGE_SMTP_HOST, MAILFORGE_SMTP_PORT, SMTP_TIMEOUT_SECONDS
from modules.gmail_api import (
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    find_gmail_message_placement,
    move_gmail_message_to_inbox,
    send_gmail_api_message,
)


GMAIL_SPAM_FOLDERS = (
    "[Gmail]/Spam",
    "[Google Mail]/Spam",
    "[Gmail]/Junk",
    "[Google Mail]/Junk",
    "Spam",
    "Junk",
    "Bulk Mail",
)
TOKEN_RE = re.compile(r"ePetrel warm verification token:\s*([A-Za-z0-9_-]{16,128})", re.IGNORECASE)
GMAIL_MODIFY_SETUP_HINT = (
    "Open Google Cloud Console > OAuth consent screen > Data access, add "
    "https://www.googleapis.com/auth/gmail.readonly and https://www.googleapis.com/auth/gmail.modify, "
    "save the consent screen, then reconnect Gmail with Full Auto Warm scopes enabled. "
    "If Google does not allow these strict permissions, reconnect with that checkbox unchecked; "
    "Gmail API sending will still work, and probing can use IMAP when configured."
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
                "message": "Automatic Gmail API scanning/rescue is off. Configure IMAP for manual scanning, or reconnect with Full Auto Warm Gmail read/rescue scopes enabled.",
            }
        return {"capable": True, "status": "gmail_modify_ready", "method": "gmail_api"}

    if sender.get("password") and sender.get("imap_host"):
        return {"capable": True, "status": "imap_best_effort_ready", "method": "imap"}

    return {
        "capable": False,
        "status": "missing_imap_move_access",
        "message": "Enable IMAP access or reconnect Gmail API with Full Auto Warm Gmail read/rescue scopes before enabling Full Auto Warm.",
    }


def _decode_header(value):
    if not value:
        return ""
    parts = []
    for content, encoding in decode_header(value):
        if isinstance(content, bytes):
            parts.append(content.decode(encoding or "utf-8", errors="ignore"))
        else:
            parts.append(content)
    return "".join(parts).strip()


def _extract_text(message):
    if message.is_multipart():
        chunks = []
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if payload:
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="ignore"))
        return "\n".join(chunks)

    payload = message.get_payload(decode=True)
    return payload.decode(message.get_content_charset() or "utf-8", errors="ignore") if payload else ""


def _folder_names(mailbox):
    folders = ["INBOX", *GMAIL_SPAM_FOLDERS]
    try:
        status, rows = mailbox.list()
    except imaplib.IMAP4.error:
        return folders
    if status != "OK":
        return folders

    for row in rows or []:
        text = row.decode("utf-8", errors="ignore") if isinstance(row, bytes) else str(row)
        match = re.search(r' "([^"]+)"$', text)
        if match:
            folders.append(match.group(1))
    return list(dict.fromkeys(folder for folder in folders if folder))


def _select(mailbox, folder, readonly=True):
    try:
        status, _ = mailbox.select(f'"{folder}"', readonly=readonly)
        return status == "OK"
    except imaplib.IMAP4.error:
        return False


def _message_matches(message, token, expected_subject=""):
    subject = _decode_header(message.get("Subject"))
    body = _extract_text(message)
    if token and token in f"{subject}\n{body}\n{message.as_string()}":
        return True
    return bool(expected_subject and expected_subject.strip() and expected_subject.strip() in subject)


def _extract_verification_token(text):
    match = TOKEN_RE.search(text or "")
    return match.group(1) if match else ""


def _placement_for_folder(folder):
    lowered = (folder or "").lower()
    if lowered == "inbox":
        return "inbox"
    if "spam" in lowered or "junk" in lowered or "bulk" in lowered:
        return "spam"
    return "other"


def _scan_imap(sender, token, subject="", limit_per_folder=60):
    password = sender.get("password")
    imap_host = sender.get("imap_host")
    imap_port = int(sender.get("imap_port") or 993)
    if not password or not imap_host:
        return {"placement": "missing", "status": "needs_imap", "error": "Missing IMAP credentials."}

    with imaplib.IMAP4_SSL(imap_host, imap_port) as mailbox:
        mailbox.login(sender["email"], password)
        for folder in _folder_names(mailbox):
            if not _select(mailbox, folder, readonly=True):
                continue
            status, messages = mailbox.search(None, "ALL")
            if status != "OK":
                continue
            mail_ids = messages[0].split()[-max(1, int(limit_per_folder or 60)) :]
            for mail_id in reversed(mail_ids):
                status, msg_data = mailbox.fetch(mail_id, "(RFC822)")
                if status != "OK":
                    continue
                for part in msg_data:
                    if not isinstance(part, tuple):
                        continue
                    message = email.message_from_bytes(part[1])
                    if _message_matches(message, token, subject):
                        body = _extract_text(message)
                        return {
                            "placement": _placement_for_folder(folder),
                            "status": "found",
                            "folder": folder,
                            "imap_mail_id": mail_id.decode("ascii", errors="ignore") if isinstance(mail_id, bytes) else str(mail_id),
                            "message_id": (message.get("Message-ID") or "").strip(),
                            "from_email": message.get("From", "").strip(),
                            "references": message.get("References", "").strip(),
                            "subject": _decode_header(message.get("Subject")),
                            "verification_token": _extract_verification_token(f"{_decode_header(message.get('Subject'))}\n{body}\n{message.as_string()}"),
                        }
    return {"placement": "missing", "status": "missing", "folder": "", "message_id": ""}


def scan_warm_account_probe(mailbox_email, token, subject=""):
    sender = get_sender(mailbox_email)
    if not sender:
        return {"placement": "missing", "status": "missing_sender", "error": "Save this Gmail sender locally before scanning."}

    has_imap_credentials = bool(sender.get("password") and sender.get("imap_host"))
    if (sender.get("auth_method") or "") == "gmail_api" and sender.get("gmail_refresh_token"):
        scopes = set(str(sender.get("gmail_granted_scopes") or "").replace(",", " ").split())
        try:
            if GMAIL_READONLY_SCOPE in scopes:
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
            imap_fallback_error = "Gmail API readonly scope is not connected for this sender."
        except Exception as exc:
            imap_fallback_error = str(exc)
            if not has_imap_credentials:
                return {
                    "placement": "",
                    "status": "gmail_api_unavailable",
                    "error": imap_fallback_error,
                    "scanner": "gmail_api",
                    "retryable_scan": True,
                }
    else:
        imap_fallback_error = ""

    if imap_fallback_error and not has_imap_credentials:
        return {
            "placement": "",
            "status": "needs_gmail_readonly_or_imap",
            "error": imap_fallback_error,
            "scanner": "gmail_api",
            "retryable_scan": False,
        }

    try:
        result = _scan_imap(sender, token, subject=subject)
        result["scanner"] = "imap"
        if imap_fallback_error:
            result["gmail_api_error"] = imap_fallback_error
        return result
    except Exception as exc:
        return {"placement": "missing", "status": "error", "error": str(exc), "scanner": "imap"}


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

    password = sender.get("password")
    imap_host = sender.get("imap_host")
    imap_port = int(sender.get("imap_port") or 993)
    source_folder = scan_result.get("folder") or ""
    mail_id = scan_result.get("imap_mail_id") or ""
    if not password or not imap_host or not source_folder or not mail_id:
        return {"moved": False, "method": "imap", "error": "missing_imap_context"}

    try:
        with imaplib.IMAP4_SSL(imap_host, imap_port) as mailbox:
            mailbox.login(sender["email"], password)
            if not _select(mailbox, source_folder, readonly=False):
                return {"moved": False, "method": "imap", "error": "source_folder_unavailable"}
            if hasattr(mailbox, "move"):
                try:
                    move_status, _ = mailbox.move(mail_id, "INBOX")
                    if move_status == "OK":
                        return {"moved": True, "method": "imap_move"}
                except imaplib.IMAP4.error:
                    pass
            copy_status, _ = mailbox.copy(mail_id, "INBOX")
            if copy_status != "OK":
                return {"moved": False, "method": "imap", "error": "copy_failed"}
            mailbox.store(mail_id, "+FLAGS", "\\Deleted")
            mailbox.expunge()
            return {"moved": True, "method": "imap"}
    except Exception as exc:
        return {"moved": False, "method": "imap", "error": str(exc)}


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
        if (sender.get("auth_method") or "") == "gmail_api":
            send_gmail_api_message(
                sender.get("gmail_client_id") or "",
                sender.get("gmail_client_secret") or "",
                sender.get("gmail_refresh_token") or "",
                msg.as_bytes(),
            )
        else:
            smtp_host = sender.get("smtp_host") or MAILFORGE_SMTP_HOST
            smtp_port = int(sender.get("smtp_port") or MAILFORGE_SMTP_PORT)
            password = sender.get("password")
            if not password:
                return {"sent": False, "error": "missing_smtp_password"}
            smtp_cls = smtplib.SMTP_SSL if smtp_port == 465 else smtplib.SMTP
            with smtp_cls(smtp_host, smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.ehlo()
                if smtp_port != 465:
                    server.starttls()
                    server.ehlo()
                server.login(sender_email, password)
                server.send_message(msg)
        return {"sent": True, "message_id": msg["Message-ID"]}
    except Exception as exc:
        return {"sent": False, "error": str(exc)}
