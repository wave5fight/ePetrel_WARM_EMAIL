import email
import imaplib
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime, parseaddr

from database.db_manager import (
    add_suppression,
    find_outbound_by_message_id,
    list_senders,
    log_delivery_event,
    log_inbound,
    log_crm_activity,
    mark_crm_bounce,
    mark_crm_inbound_reply,
    set_crm_contact_status,
    CRM_INTERESTED_FOLDER,
)
from modules.ai_agent import analyze_sentiment
from modules.email_engine import get_domain, normalize_email


UNSUBSCRIBE_KEYWORDS = (
    "unsubscribe",
    "remove me",
    "stop emailing",
    "do not contact",
    "opt out",
)

SHORT_UNSUBSCRIBE_REPLIES = {
    "no",
    "no thanks",
    "no thank you",
    "not relevant",
    "not interested",
}

BOUNCE_SENDER_HINTS = (
    "mailer-daemon",
    "postmaster",
    "mail delivery subsystem",
)

BOUNCE_SUBJECT_HINTS = (
    "delivery status notification",
    "undelivered mail returned",
    "delivery failure",
    "mail delivery failed",
    "returned mail",
    "failure notice",
)

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
STATUS_RE = re.compile(r"(?im)^Status:\s*([245]\.\d+\.\d+)")
ACTION_RE = re.compile(r"(?im)^Action:\s*([a-z-]+)")
FINAL_RECIPIENT_RE = re.compile(r"(?im)^(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*(.+)$")
FAILED_RECIPIENT_RE = re.compile(r"(?im)^X-Failed-Recipients:\s*(.+)$")
DIAGNOSTIC_RE = re.compile(r"(?im)^Diagnostic-Code:\s*(.+)$")


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
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="ignore") if payload else ""
        for part in message.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="ignore") if payload else ""
        return ""

    payload = message.get_payload(decode=True)
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="ignore") if payload else ""


def _message_to_text(message):
    parts = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="ignore"))
            elif part.get_content_type() in {"message/delivery-status", "message/rfc822"}:
                parts.append(part.as_string())
    else:
        parts.append(_extract_text(message))
    return "\n".join(parts)


def _message_datetime(raw_date):
    if not raw_date:
        return datetime.now(timezone.utc).isoformat()
    try:
        return parsedate_to_datetime(raw_date).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def _first_email(value):
    match = EMAIL_RE.search(value or "")
    return normalize_email(match.group(0)) if match else ""


def _is_unsubscribe_reply(subject, content):
    lowered = f"{subject}\n{content}".lower()
    if any(keyword in lowered for keyword in UNSUBSCRIBE_KEYWORDS):
        return True
    normalized_body = re.sub(r"[\s\W_]+", " ", content or "").strip().lower()
    return normalized_body in SHORT_UNSUBSCRIBE_REPLIES


def _extract_original_message_id(message):
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_type() != "message/rfc822":
            continue
        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            original_id = payload[0].get("Message-ID", "").strip()
            if original_id:
                return original_id
        original_id = part.get("Message-ID", "").strip()
        if original_id:
            return original_id
    return ""


def _parse_bounce(message, sender_email, subject, content):
    lowered_subject = (subject or "").lower()
    lowered_sender = (sender_email or "").lower()
    raw_text = _message_to_text(message)
    combined = f"{subject}\n{content}\n{raw_text}"

    looks_like_bounce = (
        any(hint in lowered_subject for hint in BOUNCE_SUBJECT_HINTS)
        or any(hint in lowered_sender for hint in BOUNCE_SENDER_HINTS)
        or "message/delivery-status" in raw_text.lower()
    )
    if not looks_like_bounce:
        return None

    status_match = STATUS_RE.search(combined)
    action_match = ACTION_RE.search(combined)
    diagnostic_match = DIAGNOSTIC_RE.search(combined)
    failed_match = FAILED_RECIPIENT_RE.search(combined)
    recipient_match = FINAL_RECIPIENT_RE.search(combined)

    failed_recipient = ""
    if failed_match:
        failed_recipient = _first_email(failed_match.group(1))
    if not failed_recipient and recipient_match:
        failed_recipient = _first_email(recipient_match.group(1))

    status_code = status_match.group(1) if status_match else ""
    action = action_match.group(1).lower() if action_match else ""
    original_message_id = _extract_original_message_id(message)
    outbound = find_outbound_by_message_id(original_message_id)

    if not failed_recipient and outbound:
        failed_recipient = outbound.get("receiver", "")

    if status_code.startswith("5") or action == "failed":
        event_type = "bounced_hard"
        severity = "critical"
    elif status_code.startswith("4") or action == "delayed":
        event_type = "bounced_soft"
        severity = "warning"
    else:
        event_type = "bounced"
        severity = "warning"

    details = " | ".join(
        item
        for item in [
            f"status={status_code}" if status_code else "",
            f"action={action}" if action else "",
            diagnostic_match.group(1).strip() if diagnostic_match else "",
        ]
        if item
    )

    return {
        "event_type": event_type,
        "severity": severity,
        "failed_recipient": failed_recipient,
        "original_message_id": original_message_id,
        "outbound": outbound,
        "details": details,
    }


def _imap_mailbox_arg(folder):
    return f'"{str(folder or "INBOX").replace(chr(34), "")}"'


def _ensure_imap_folder(mailbox, folder):
    status, _ = mailbox.create(folder)
    return status in {"OK", "NO"}


def _move_message_to_folder(mailbox, uid, folder=CRM_INTERESTED_FOLDER):
    if not uid:
        return "missing uid"
    try:
        _ensure_imap_folder(mailbox, folder)
        destination = _imap_mailbox_arg(folder)
        status, _ = mailbox.uid("MOVE", uid, destination)
        if status == "OK":
            return ""
        status, _ = mailbox.uid("COPY", uid, destination)
        if status != "OK":
            return f"IMAP COPY to {folder} failed"
        mailbox.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        mailbox.expunge()
        return ""
    except Exception as exc:
        return str(exc)


def fetch_inbox_for_sender(sender, limit=25):
    """Fetch recent inbox messages for one configured sender and store new replies."""
    email_address = sender["email"]
    password = sender.get("password")
    imap_host = sender.get("imap_host")
    imap_port = int(sender.get("imap_port") or 993)
    if not password or not imap_host:
        return {"sender": email_address, "stored": 0, "error": "Missing IMAP credentials"}

    stored = 0
    try:
        with imaplib.IMAP4_SSL(imap_host, imap_port) as mailbox:
            mailbox.login(email_address, password)
            mailbox.select("INBOX")
            status, messages = mailbox.uid("SEARCH", None, "ALL")
            if status != "OK":
                return {"sender": email_address, "stored": 0, "error": "IMAP search failed"}

            mail_uids = messages[0].split()[-limit:]
            for mail_uid in mail_uids:
                uid_text = mail_uid.decode("ascii", errors="ignore") if isinstance(mail_uid, bytes) else str(mail_uid)
                status, msg_data = mailbox.uid("FETCH", mail_uid, "(RFC822)")
                if status != "OK":
                    continue
                for response_part in msg_data:
                    if not isinstance(response_part, tuple):
                        continue
                    message = email.message_from_bytes(response_part[1])
                    sender_email = normalize_email(parseaddr(message.get("From", ""))[1])
                    subject = _decode_header(message.get("Subject"))
                    content = _extract_text(message)
                    message_id = message.get("Message-ID", "").strip()
                    bounce = _parse_bounce(message, sender_email, subject, content)
                    if bounce:
                        outbound = bounce.get("outbound") or {}
                        failed_recipient = bounce.get("failed_recipient") or ""
                        if bounce["event_type"] == "bounced_hard" and failed_recipient:
                            add_suppression(failed_recipient, "hard_bounce")
                            mark_crm_bounce(
                                failed_recipient,
                                reason="hard_bounce",
                                event_time=_message_datetime(message.get("Date")),
                            )
                        log_delivery_event(
                            bounce["event_type"],
                            sender=outbound.get("sender", email_address),
                            receiver=failed_recipient,
                            source="imap_bounce",
                            subject=subject,
                            message_id=bounce.get("original_message_id", ""),
                            source_message_id=message_id,
                            target_domain=outbound.get("target_domain") or get_domain(failed_recipient),
                            severity=bounce["severity"],
                            details=bounce.get("details", ""),
                            event_time=_message_datetime(message.get("Date")),
                        )
                        continue

                    sentiment = analyze_sentiment(content)
                    message_time = _message_datetime(message.get("Date"))

                    if _is_unsubscribe_reply(subject, content):
                        add_suppression(sender_email, "unsubscribe_reply")
                        set_crm_contact_status(
                            sender_email,
                            "not_interested",
                            note="unsubscribe keyword detected in reply",
                            actor="imap",
                        )
                        log_delivery_event(
                            "unsubscribe",
                            sender=email_address,
                            receiver=sender_email,
                            source="imap_reply",
                            subject=subject,
                            message_id="",
                            source_message_id=message_id,
                            target_domain=get_domain(sender_email),
                            severity="warning",
                            details="unsubscribe keyword detected in reply",
                            event_time=_message_datetime(message.get("Date")),
                        )
                        sentiment = "[Refused]"

                    inserted = log_inbound(
                        message_time,
                        sender_email,
                        email_address,
                        subject,
                        content,
                        sentiment,
                        message_id=message_id,
                        imap_uid=uid_text,
                        imap_folder="INBOX",
                    )
                    if inserted:
                        crm_status = mark_crm_inbound_reply(
                            sender_email,
                            inbound_email_id=inserted,
                            receiver=email_address,
                            subject=subject,
                            content=content,
                            sentiment=sentiment,
                            event_time=message_time,
                        )
                        if crm_status == "interested":
                            move_error = _move_message_to_folder(mailbox, mail_uid, CRM_INTERESTED_FOLDER)
                            log_crm_activity(
                                sender_email,
                                "imap_move",
                                (
                                    f"Moved to {CRM_INTERESTED_FOLDER}"
                                    if not move_error
                                    else f"Move to {CRM_INTERESTED_FOLDER} failed: {move_error}"
                                ),
                                status="ok" if not move_error else "failed",
                                actor="imap",
                                inbound_email_id=inserted,
                                channel="email",
                                metadata={"folder": CRM_INTERESTED_FOLDER, "error": move_error},
                            )
                        stored += 1
        return {"sender": email_address, "stored": stored, "error": ""}
    except Exception as exc:
        return {"sender": email_address, "stored": stored, "error": str(exc)}


def fetch_all_inboxes(limit_per_sender=25):
    results = []
    for sender in list_senders(include_credentials=True):
        if sender["status"] != "active":
            continue
        results.append(fetch_inbox_for_sender(sender, limit=limit_per_sender))
    return results
