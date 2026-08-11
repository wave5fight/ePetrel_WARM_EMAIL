import email
import imaplib
import sqlite3
from email.header import decode_header
from email.utils import parsedate_to_datetime

from config import DB_PATH
from database.db_manager import list_seed_accounts, log_delivery_event, mark_seed_checked


FALLBACK_SPAM_FOLDERS = ("Spam", "Junk", "Junk Email", "Bulk Mail", "[Gmail]/Spam")
SEED_LOOKBACK_DAYS = 7
MISSING_GRACE_MINUTES = 10


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


def _message_datetime(raw_date):
    if not raw_date:
        return None
    try:
        return parsedate_to_datetime(raw_date).isoformat()
    except (TypeError, ValueError):
        return None


def _select_folder(mailbox, folder):
    if not folder:
        return False
    candidates = [folder, f'"{folder}"'] if " " in folder or "/" in folder else [folder]
    for candidate in candidates:
        status, _ = mailbox.select(candidate, readonly=True)
        if status == "OK":
            return True
    return False


def _recent_seed_outbounds(seed_email):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, timestamp, sender, receiver, subject, message_id, target_domain
        FROM outbound_logs
        WHERE status = 'success'
          AND receiver = ?
          AND message_id IS NOT NULL
          AND message_id != ''
          AND datetime(timestamp) >= datetime('now', ?)
        ORDER BY timestamp DESC
        """,
        (seed_email.strip().lower(), f"-{SEED_LOOKBACK_DAYS} days"),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def _missing_seed_outbounds(seed_email):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, timestamp, sender, receiver, subject, message_id, target_domain
        FROM outbound_logs
        WHERE status = 'success'
          AND receiver = ?
          AND message_id IS NOT NULL
          AND message_id != ''
          AND datetime(timestamp) >= datetime('now', ?)
          AND datetime(timestamp) <= datetime('now', ?)
        ORDER BY timestamp DESC
        """,
        (
            seed_email.strip().lower(),
            f"-{SEED_LOOKBACK_DAYS} days",
            f"-{MISSING_GRACE_MINUTES} minutes",
        ),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def _scan_folder(mailbox, folder, placement, outbounds_by_message_id, limit):
    if not _select_folder(mailbox, folder):
        return {"folder": folder, "checked": 0, "matched": 0, "message_ids": set(), "error": "select failed"}

    status, messages = mailbox.search(None, "ALL")
    if status != "OK":
        return {"folder": folder, "checked": 0, "matched": 0, "message_ids": set(), "error": "search failed"}

    checked = 0
    matched = 0
    matched_ids = set()
    for mail_id in messages[0].split()[-limit:]:
        status, msg_data = mailbox.fetch(mail_id, "(RFC822)")
        if status != "OK":
            continue
        for response_part in msg_data:
            if not isinstance(response_part, tuple):
                continue
            message = email.message_from_bytes(response_part[1])
            checked += 1
            message_id = message.get("Message-ID", "").strip()
            outbound = outbounds_by_message_id.get(message_id)
            if not outbound:
                continue
            matched += 1
            matched_ids.add(message_id)
            log_delivery_event(
                f"seed_{placement}",
                sender=outbound.get("sender", ""),
                receiver=outbound.get("receiver", ""),
                source="seed_imap",
                subject=_decode_header(message.get("Subject")) or outbound.get("subject", ""),
                message_id=message_id,
                source_message_id=message_id,
                target_domain=outbound.get("target_domain", ""),
                severity="critical" if placement == "spam" else "info",
                details=f"seed placement={placement}; folder={folder}",
                event_time=_message_datetime(message.get("Date")),
            )

    return {"folder": folder, "checked": checked, "matched": matched, "message_ids": matched_ids, "error": ""}


def check_seed_account(account, limit_per_folder=50):
    seed_email = account["email"].strip().lower()
    password = account.get("password")
    imap_host = account.get("imap_host")
    imap_port = int(account.get("imap_port") or 993)
    if not password or not imap_host:
        return {"seed": seed_email, "matched": 0, "missing": 0, "error": "Missing seed IMAP credentials"}

    outbounds = _recent_seed_outbounds(seed_email)
    outbounds_by_message_id = {row["message_id"]: row for row in outbounds if row.get("message_id")}
    found_ids = set()
    matched = 0
    folder_results = []

    try:
        with imaplib.IMAP4_SSL(imap_host, imap_port) as mailbox:
            mailbox.login(seed_email, password)
            folder_results.append(
                _scan_folder(
                    mailbox,
                    account.get("inbox_folder") or "INBOX",
                    "inbox",
                    outbounds_by_message_id,
                    limit_per_folder,
                )
            )

            spam_folders = []
            configured_spam = account.get("spam_folder") or "Spam"
            for folder in (configured_spam, *FALLBACK_SPAM_FOLDERS):
                if folder and folder not in spam_folders:
                    spam_folders.append(folder)
            for spam_folder in spam_folders:
                result = _scan_folder(mailbox, spam_folder, "spam", outbounds_by_message_id, limit_per_folder)
                folder_results.append(result)
                if not result["error"]:
                    break

        for result in folder_results:
            matched += result["matched"]
            found_ids.update(result["message_ids"])

        missing = 0
        for outbound in _missing_seed_outbounds(seed_email):
            message_id = outbound.get("message_id", "")
            if not message_id or message_id in found_ids:
                continue
            if log_delivery_event(
                "seed_missing",
                sender=outbound.get("sender", ""),
                receiver=outbound.get("receiver", ""),
                source="seed_imap",
                subject=outbound.get("subject", ""),
                message_id=message_id,
                source_message_id=f"missing:{message_id}",
                target_domain=outbound.get("target_domain", ""),
                severity="warning",
                details=f"not found in seed folders after {MISSING_GRACE_MINUTES} minutes",
            ):
                missing += 1

        mark_seed_checked(seed_email)
        return {
            "seed": seed_email,
            "matched": matched,
            "missing": missing,
            "folders": folder_results,
            "error": "",
        }
    except Exception as exc:
        return {"seed": seed_email, "matched": matched, "missing": 0, "folders": folder_results, "error": str(exc)}


def check_all_seed_accounts(limit_per_folder=50):
    return [
        check_seed_account(account, limit_per_folder=limit_per_folder)
        for account in list_seed_accounts(include_credentials=True, active_only=True)
    ]
