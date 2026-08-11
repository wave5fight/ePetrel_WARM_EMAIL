import imaplib
import smtplib

from config import SMTP_TIMEOUT_SECONDS


def _short_error(exc):
    message = str(exc).strip()
    if not message:
        message = exc.__class__.__name__
    return message[:240]


def check_smtp_login(email, password, host, port):
    try:
        smtp_cls = smtplib.SMTP_SSL if int(port) == 465 else smtplib.SMTP
        with smtp_cls(host, int(port), timeout=SMTP_TIMEOUT_SECONDS) as server:
            server.ehlo()
            if int(port) != 465:
                server.starttls()
                server.ehlo()
            server.login(email, password)
        return {"status": "passed", "error": ""}
    except Exception as exc:
        return {"status": "failed", "error": _short_error(exc)}


def check_imap_login(email, password, host, port):
    try:
        if int(port) == 993:
            with imaplib.IMAP4_SSL(host, int(port), timeout=SMTP_TIMEOUT_SECONDS) as mailbox:
                mailbox.login(email, password)
                mailbox.logout()
        else:
            with imaplib.IMAP4(host, int(port), timeout=SMTP_TIMEOUT_SECONDS) as mailbox:
                mailbox.starttls()
                mailbox.login(email, password)
                mailbox.logout()
        return {"status": "passed", "error": ""}
    except Exception as exc:
        return {"status": "failed", "error": _short_error(exc)}


def check_sender_mailbox(email, password, smtp_host, smtp_port, imap_host, imap_port):
    smtp_result = check_smtp_login(email, password, smtp_host, smtp_port)
    imap_result = check_imap_login(email, password, imap_host, imap_port)
    mailbox_status = "passed" if smtp_result["status"] == "passed" and imap_result["status"] == "passed" else "failed"
    errors = [
        f"SMTP: {smtp_result['error']}" if smtp_result["error"] else "",
        f"IMAP: {imap_result['error']}" if imap_result["error"] else "",
    ]
    return {
        "smtp": smtp_result["status"],
        "imap": imap_result["status"],
        "mailbox": mailbox_status,
        "error": " | ".join(item for item in errors if item),
    }
