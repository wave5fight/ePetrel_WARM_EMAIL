import asyncio
import json
import logging
import random
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from config import (
    MAIL_FROM_NAME,
    MAILFORGE_SMTP_HOST,
    MAILFORGE_SMTP_PORT,
    SMTP_TIMEOUT_SECONDS,
    WARM_SEND_MAX_GAP_SECONDS,
    WARM_SEND_MIN_GAP_SECONDS,
    WARM_TASK_CLAIM_LIMIT,
    WARM_WORKER_INTERVAL_SECONDS,
)
from database.db_manager import (
    get_sender,
    get_warm_cluster,
    get_warm_local_task,
    list_warm_mailboxes,
    log_warm_event,
    mark_warm_cluster_dissolved,
    update_warm_local_task,
    upsert_secret_app_setting,
    upsert_warm_worker_state,
    upsert_warm_local_task,
    upsert_warm_local_thread,
)
from modules.email_engine import normalize_email
from modules.gmail_api import send_gmail_api_message
from modules.safe_logging import mask_email, redact_sensitive
from modules.warm_account_probe import scan_warm_account_probe
from modules.warm_client import detect_provider, make_owner_signature, warm_policy_config
from modules.warm_content import generate_warm_content
from modules.warm_service import WarmApiError, claim_warm_tasks, register_warm_mailbox, report_warm_task, send_warm_heartbeat


logger = logging.getLogger("epetrel.warm_worker")
_auth_data = {}
_worker_task = None
_stop_event = None
_last_message_action_at = {}
WARM_AUTH_SETTING_KEY = "warm_auth_json"


def set_warm_worker_auth(auth_data):
    global _auth_data
    if isinstance(auth_data, dict) and auth_data.get("access_token"):
        _auth_data = auth_data
    elif not auth_data:
        _auth_data = {}


def _warm_api_error_is_invalid_token(exc):
    message = str(exc).lower()
    return "invalid warm token" in message or "warm token expired" in message


def _warm_api_error_is_cluster_dissolved(exc):
    message = str(exc).lower()
    return "cluster_dissolved" in message or "warm cluster has been dissolved" in message or "warm 群已被群主解散" in message


def _clear_invalid_warm_auth(exc):
    global _auth_data
    if not _warm_api_error_is_invalid_token(exc):
        return False
    _auth_data = {}
    upsert_secret_app_setting(WARM_AUTH_SETTING_KEY, "")
    logger.warning("warm auth cleared reason=%s", redact_sensitive(str(exc)))
    return True


def _apply_auth_renewal(response):
    global _auth_data
    if not isinstance(response, dict) or not _auth_data.get("access_token"):
        return
    expires_at = response.get("auth_expires_at")
    expires_in = response.get("auth_expires_in")
    try:
        expires_at = float(expires_at or 0)
    except (TypeError, ValueError):
        expires_at = 0
    try:
        expires_in = int(expires_in or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_at <= 0 and expires_in <= 0:
        return
    now = time.time()
    data = dict(_auth_data)
    if expires_at > 0:
        data["_expires_at"] = expires_at
        data["expires_in"] = max(0, int(expires_at - now))
    else:
        data["expires_in"] = expires_in
        data["_expires_at"] = now + max(0, expires_in)
    data["_stored_at"] = now
    _auth_data = data
    upsert_secret_app_setting(WARM_AUTH_SETTING_KEY, json.dumps(data, ensure_ascii=True))


def start_warm_worker():
    global _worker_task, _stop_event
    if _worker_task and not _worker_task.done():
        return _worker_task
    _stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(_warm_worker_loop(), name="epetrel-warm-worker")
    return _worker_task


async def stop_warm_worker():
    if _stop_event:
        _stop_event.set()
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass


async def _warm_worker_loop():
    interval = max(30, int(WARM_WORKER_INTERVAL_SECONDS or 300))
    while True:
        try:
            result = await run_warm_worker_once()
            status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
            reason = result.get("reason", "") if isinstance(result, dict) else ""
            completed_count = len(result.get("completed") or []) if isinstance(result, dict) else 0
            logger.info("warm worker cycle status=%s reason=%s completed=%s", status, reason, completed_count)
        except Exception as exc:  # pragma: no cover - worker must not crash the app
            logger.exception("warm worker cycle failed: %s", redact_sensitive(str(exc)))
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue


async def run_warm_worker_once():
    auth = _auth_data if isinstance(_auth_data, dict) else {}
    token = auth.get("access_token", "")
    if not token:
        logger.info("warm worker idle reason=missing_auth")
        return {"status": "idle", "reason": "missing_auth"}

    active_mailboxes = [
        row
        for row in list_warm_mailboxes()
        if row.get("status") == "active" and row.get("cluster_id") and normalize_email(row.get("email", ""))
        and (get_warm_cluster(row.get("cluster_id", "")).get("status") != "dissolved")
    ]
    if not active_mailboxes:
        logger.info("warm worker idle reason=no_active_mailboxes")
        return {"status": "idle", "reason": "no_active_mailboxes"}

    by_cluster = {}
    for mailbox in active_mailboxes:
        by_cluster.setdefault(mailbox["cluster_id"], []).append(mailbox)

    completed = []
    for cluster_id, mailboxes in by_cluster.items():
        ready_mailboxes = []
        for mailbox in mailboxes:
            if await _sync_remote_mailbox_policy(token, cluster_id, mailbox):
                ready_mailboxes.append(mailbox)
        if not ready_mailboxes:
            continue
        emails = [row["email"] for row in ready_mailboxes]
        try:
            heartbeat_response = await asyncio.to_thread(
                send_warm_heartbeat,
                token,
                {
                    "cluster_id": cluster_id,
                    "mailboxes": emails,
                    "capabilities": ["send", "scan", "reply", "inbox_rescue"],
                    "mailbox_policies": {
                        row["email"]: _mailbox_policy_payload(row)
                        for row in ready_mailboxes
                    },
                },
            )
        except WarmApiError as exc:
            auth_cleared = _clear_invalid_warm_auth(exc)
            cluster_dissolved = _warm_api_error_is_cluster_dissolved(exc)
            if cluster_dissolved:
                mark_warm_cluster_dissolved(cluster_id)
            logger.warning("warm heartbeat failed cluster=%s mailboxes=%s error=%s", cluster_id, len(emails), redact_sensitive(str(exc)))
            for mailbox in ready_mailboxes:
                upsert_warm_worker_state(
                    cluster_id,
                    mailbox["email"],
                    status="cluster_dissolved" if cluster_dissolved else "heartbeat_failed",
                    error="" if cluster_dissolved else ("warm auth expired; reauthorize warm" if auth_cleared else str(exc)),
                    claim_message="This warm cluster has been dissolved by the owner. 该 Warm 群已被群主解散。" if cluster_dissolved else "",
                    heartbeat=True,
                    details={"mailboxes": len(emails)},
                )
            if auth_cleared:
                return {"status": "idle", "reason": "warm_auth_invalid"}
            continue
        _apply_auth_renewal(heartbeat_response)
        heartbeat_scheduler = str((heartbeat_response or {}).get("scheduler") or "")
        for mailbox in ready_mailboxes:
            upsert_warm_worker_state(
                cluster_id,
                mailbox["email"],
                status="heartbeat_ok",
                scheduler=heartbeat_scheduler,
                heartbeat=True,
            )
        for mailbox in ready_mailboxes:
            claim_response = await _claim_tasks(token, cluster_id, mailbox)
            tasks = claim_response.get("tasks") or []
            claim_message = str(claim_response.get("message") or "")
            scheduler = str(claim_response.get("scheduler") or heartbeat_scheduler or "")
            upsert_warm_worker_state(
                cluster_id,
                mailbox["email"],
                status="claimed" if tasks else "claim_empty",
                scheduler=scheduler,
                claim_message=claim_message,
                tasks_claimed=len(tasks),
                claimed=True,
                error=claim_response.get("error", ""),
                details={"policy": claim_response.get("policy") or {}},
            )
            logger.info("warm task claim mailbox=%s cluster=%s tasks=%s", mask_email(mailbox["email"]), cluster_id, len(tasks))
            for task in tasks:
                await _pace_message_task(mailbox["email"], task)
                result = await _execute_task(token, task, mailbox["email"])
                completed.append(result)
                upsert_warm_worker_state(
                    cluster_id,
                    mailbox["email"],
                    status=f"task_{result.get('status', 'done')}",
                    scheduler=scheduler,
                    completed_count=1,
                    success=result.get("status") not in {"failed", "skipped"},
                    error=result.get("error", "") if result.get("status") == "failed" else "",
                    details={"task_id": result.get("task_id", ""), "task_type": task.get("task_type") or task.get("type") or ""},
                )
    return {"status": "ok", "completed": completed}


async def _sync_remote_mailbox_policy(token, cluster_id, mailbox):
    email = normalize_email(mailbox.get("email", ""))
    if not email:
        return
    policy_payload = _mailbox_policy_payload(mailbox)
    payload = {
        "cluster_id": cluster_id,
        "email": email,
        "provider": mailbox.get("provider") or detect_provider(email),
        "status": mailbox.get("status") or "active",
        "capabilities": ["send", "scan", "reply", "inbox_rescue"],
        **policy_payload,
    }
    cluster = get_warm_cluster(cluster_id, include_secrets=True)
    if cluster.get("owner_private_key"):
        payload.update(make_owner_signature(cluster.get("owner_private_key", ""), cluster_id, "approve", email))
        payload["owner_email"] = cluster.get("owner_email", "")
    try:
        await asyncio.to_thread(register_warm_mailbox, token, payload)
    except WarmApiError as exc:
        if _clear_invalid_warm_auth(exc):
            raise
        if _warm_api_error_is_cluster_dissolved(exc):
            mark_warm_cluster_dissolved(cluster_id)
            upsert_warm_worker_state(
                cluster_id,
                email,
                status="cluster_dissolved",
                claim_message="This warm cluster has been dissolved by the owner. 该 Warm 群已被群主解散。",
                error="",
                details={"policy": policy_payload},
            )
            logger.warning("warm mailbox policy sync stopped because cluster dissolved mailbox=%s", mask_email(email))
            return False
        if "verify this warm mailbox" in str(exc).lower():
            upsert_warm_worker_state(
                cluster_id,
                email,
                status="policy_sync_required",
                claim_message="Remote policy sync requires mailbox re-verification.",
                error="",
                details={"policy": policy_payload},
            )
            logger.warning("warm mailbox policy sync requires verification mailbox=%s", mask_email(email))
            return False
        logger.warning("warm mailbox policy sync failed mailbox=%s error=%s", mask_email(email), redact_sensitive(str(exc)))
    return True


def _mailbox_policy_payload(mailbox):
    policy = warm_policy_config()
    sleep_start = int(policy.get("sleep_start_hour") or 0)
    sleep_end = int(policy.get("sleep_end_hour") or 0)
    avoid_sleep = bool(int(mailbox.get("avoid_sleep_hours") or 0)) and sleep_start != sleep_end
    return {
        "daily_limit": int(mailbox.get("daily_limit") or 5),
        "timezone": mailbox.get("timezone") or policy.get("timezone", ""),
        "scan_soft_timeout_hours": int(mailbox.get("scan_soft_timeout_hours") or policy.get("scan_soft_timeout_hours") or 24),
        "scan_hard_timeout_hours": int(mailbox.get("scan_hard_timeout_hours") or policy.get("scan_hard_timeout_hours") or 48),
        "reply_min_delay_hours": int(mailbox.get("reply_min_delay_hours") or policy.get("reply_min_delay_hours") or 2),
        "reply_hard_timeout_hours": int(mailbox.get("reply_hard_timeout_hours") or policy.get("reply_hard_timeout_hours") or 48),
        "sleep_start_hour": sleep_start,
        "sleep_end_hour": sleep_end,
        "avoid_sleep_hours": avoid_sleep,
        "avoid_weekends": bool(int(mailbox.get("avoid_weekends") or 0)),
    }


def _task_sends_message(task):
    task_type = str(task.get("task_type") or task.get("type") or "send").strip()
    return task_type in {"send", "send_initial", "initial_send", "reply", "send_reply"}


async def _pace_message_task(mailbox_email, task):
    if not _task_sends_message(task):
        return
    email = normalize_email(mailbox_email)
    if not email:
        return
    min_gap = max(0, int(WARM_SEND_MIN_GAP_SECONDS or 0))
    max_gap = max(min_gap, int(WARM_SEND_MAX_GAP_SECONDS or min_gap))
    last_at = float(_last_message_action_at.get(email) or 0)
    if last_at:
        gap = random.uniform(min_gap, max_gap)
        wait_seconds = last_at + gap - time.time()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
    _last_message_action_at[email] = time.time()


async def _claim_tasks(token, cluster_id, mailbox):
    mailbox_email = normalize_email(mailbox.get("email", ""))
    policy_payload = _mailbox_policy_payload(mailbox)
    try:
        claim_limit = max(1, min(int(WARM_TASK_CLAIM_LIMIT or 1), 3))
        response = await asyncio.to_thread(
            claim_warm_tasks,
            token,
            {
                "cluster_id": cluster_id,
                "mailbox_email": mailbox_email,
                "from_email": mailbox_email,
                "limit": claim_limit,
                "policy": policy_payload,
                **policy_payload,
            },
        )
    except WarmApiError as exc:
        _clear_invalid_warm_auth(exc)
        if _warm_api_error_is_cluster_dissolved(exc):
            mark_warm_cluster_dissolved(cluster_id)
        logger.warning("warm task claim failed mailbox=%s error=%s", mask_email(mailbox_email), redact_sensitive(str(exc)))
        return {"tasks": [], "message": "", "error": str(exc)}
    return response if isinstance(response, dict) else {"tasks": []}


async def _execute_task(token, task, fallback_mailbox):
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return {"status": "skipped", "error": "missing_task_id"}
    task_type = str(task.get("task_type") or task.get("type") or "send").strip()
    cluster_id = str(task.get("cluster_id") or "").strip()
    mailbox_email = normalize_email(task.get("mailbox_email") or task.get("from_email") or fallback_mailbox)
    peer_email = normalize_email(task.get("to_email") or task.get("receiver_email") or task.get("peer_email"))
    existing = get_warm_local_task(task_id)
    if existing.get("status") in {"sent", "scanned", "replied", "reported"}:
        await _report_existing(token, task, existing)
        return {"task_id": task_id, "status": "already_done"}

    upsert_warm_local_task(task_id, cluster_id, task_type, mailbox_email, peer_email, task, status="claimed")
    try:
        if task_type in {"send", "send_initial", "initial_send"}:
            return await _send_initial(token, task, mailbox_email, peer_email)
        if task_type in {"scan", "scan_placement"}:
            return await _scan_placement(token, task, mailbox_email)
        if task_type in {"reply", "send_reply"}:
            return await _send_reply(token, task, mailbox_email, peer_email)
        update_warm_local_task(task_id, status="failed", error=f"unsupported task_type {task_type}")
        return {"task_id": task_id, "status": "failed", "error": "unsupported_task_type"}
    except Exception as exc:
        update_warm_local_task(task_id, status="failed", error=str(exc))
        await _report(token, task, "failed", mailbox_email, message_id="", placement="", details={"error": str(exc)})
        logger.warning("warm task failed task=%s mailbox=%s error=%s", task_id, mask_email(mailbox_email), redact_sensitive(str(exc)))
        return {"task_id": task_id, "status": "failed", "error": str(exc)}


async def _send_initial(token, task, sender_email, receiver_email):
    task_id = task["task_id"]
    if not sender_email or not receiver_email:
        raise RuntimeError("sender and receiver are required")
    content = generate_warm_content(
        task_id=task_id,
        cluster_id=task.get("cluster_id", ""),
        provider=task.get("provider", ""),
        stage=task.get("content_stage") or task.get("stage") or "initial_send",
        use_llm=True,
        require_llm=True,
        sender_email=sender_email,
        receiver_email=receiver_email,
        scenario_seed=task.get("scenario_seed") or task.get("warm_token") or "",
        ensure_unique=True,
    )
    result = await asyncio.to_thread(
        _send_plain_message,
        sender_email,
        receiver_email,
        content["subject"],
        content["body"],
        _warm_tracking_headers(task),
    )
    message_id = result.get("message_id", "")
    update_warm_local_task(task_id, status="sent", message_id=message_id)
    thread_id = task.get("thread_id") or task_id
    upsert_warm_local_thread(
        thread_id,
        cluster_id=task.get("cluster_id", ""),
        sender_email=sender_email,
        peer_email=receiver_email,
        subject=content["subject"],
        last_message_id=message_id,
        topic=(content.get("recipe") or {}).get("topic", ""),
        persona=(content.get("recipe") or {}).get("persona", ""),
        context={"last_body": content["body"], "source": content.get("source", "")},
    )
    log_warm_event(
        cluster_id=task.get("cluster_id", ""),
        mailbox_email=sender_email,
        task_id=task_id,
        event_type="sent",
        status="sent",
        message_id=message_id,
        details="local warm worker send",
    )
    await _report(
        token,
        task,
        "sent",
        sender_email,
        message_id=message_id,
        details={"content_source": content.get("source", ""), "subject": content["subject"]},
    )
    return {"task_id": task_id, "status": "sent", "message_id": message_id}


async def _scan_placement(token, task, mailbox_email):
    lookup = task.get("warm_token") or task.get("message_id") or task.get("subject_hash") or ""
    result = await asyncio.to_thread(scan_warm_account_probe, mailbox_email, lookup, task.get("subject", ""))
    placement = result.get("placement", "")
    retryable_scan = bool(result.get("retryable_scan"))
    report_event_type = "failed" if retryable_scan else "placement"
    local_status = "scan_retryable" if retryable_scan else "scanned"
    update_warm_local_task(task["task_id"], status=local_status, message_id=result.get("message_id", ""), placement=placement)
    log_warm_event(
        cluster_id=task.get("cluster_id", ""),
        mailbox_email=mailbox_email,
        task_id=task["task_id"],
        event_type=report_event_type,
        status=result.get("status", local_status),
        placement=placement,
        message_id=result.get("rfc822_message_id") or result.get("message_id", ""),
        details=str(result),
    )
    await _report(
        token,
        task,
        report_event_type,
        mailbox_email,
        message_id=result.get("rfc822_message_id") or result.get("message_id", ""),
        placement=placement,
        details=result,
    )
    return {"task_id": task["task_id"], "status": local_status, "placement": placement, "retryable_scan": retryable_scan}


async def _send_reply(token, task, sender_email, receiver_email):
    subject = task.get("subject") or task.get("original_subject") or "Quick note"
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    content = generate_warm_content(
        task_id=task["task_id"],
        cluster_id=task.get("cluster_id", ""),
        provider=task.get("provider", ""),
        stage=task.get("content_stage") or "reply_1",
        previous_messages=task.get("previous_messages") if isinstance(task.get("previous_messages"), list) else [],
        use_llm=True,
        require_llm=True,
        sender_email=sender_email,
        receiver_email=receiver_email,
        scenario_seed=task.get("scenario_seed") or "",
        ensure_unique=True,
    )
    headers = _warm_tracking_headers(task)
    original_message_id = task.get("message_id") or task.get("original_message_id") or ""
    references = " ".join(item for item in [task.get("references", ""), original_message_id] if item).strip()
    if original_message_id:
        headers["In-Reply-To"] = original_message_id
    if references:
        headers["References"] = references
    result = await asyncio.to_thread(_send_plain_message, sender_email, receiver_email, subject or content["subject"], content["body"], headers)
    update_warm_local_task(task["task_id"], status="replied", message_id=result.get("message_id", ""))
    log_warm_event(
        cluster_id=task.get("cluster_id", ""),
        mailbox_email=sender_email,
        task_id=task["task_id"],
        event_type="reply",
        status="replied",
        message_id=result.get("message_id", ""),
        details="local warm worker reply",
    )
    await _report(
        token,
        task,
        "reply",
        sender_email,
        message_id=result.get("message_id", ""),
        placement=task.get("placement", "inbox"),
        details={"content_source": content.get("source", ""), "subject": subject or content["subject"]},
    )
    return {"task_id": task["task_id"], "status": "replied", "message_id": result.get("message_id", "")}


def _warm_tracking_headers(task):
    headers = {}
    cluster_id = str(task.get("cluster_id") or "").strip()
    warm_token = str(task.get("warm_token") or "").strip()
    task_id = str(task.get("task_id") or "").strip()
    if cluster_id:
        headers["X-ePetrel-Warm-Cluster-ID"] = cluster_id
    if warm_token:
        headers["X-ePetrel-Warm-Token"] = warm_token
    if task_id:
        headers["X-ePetrel-Warm-Task-ID"] = task_id
    return headers


def _send_plain_message(sender_email, receiver_email, subject, body, headers=None):
    sender = get_sender(sender_email)
    if not sender:
        raise RuntimeError("missing_sender")
    sender_domain = sender_email.split("@", 1)[1] if "@" in sender_email else "localhost"
    msg = EmailMessage()
    msg["From"] = formataddr((sender.get("from_name") or MAIL_FROM_NAME, sender_email))
    msg["To"] = receiver_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_domain)
    allowed_headers = {
        "In-Reply-To",
        "References",
        "X-ePetrel-Warm-Cluster-ID",
        "X-ePetrel-Warm-Token",
        "X-ePetrel-Warm-Task-ID",
    }
    for name, value in (headers or {}).items():
        if name in allowed_headers and value:
            msg[name] = str(value).replace("\r", " ").replace("\n", " ").strip()
    msg.set_content(body)

    if (sender.get("auth_method") or "") == "gmail_api":
        send_gmail_api_message(
            sender.get("gmail_client_id") or "",
            sender.get("gmail_client_secret") or "",
            sender.get("gmail_refresh_token") or "",
            msg.as_bytes(),
        )
    else:
        password = sender.get("password")
        if not password:
            raise RuntimeError("missing_smtp_password")
        smtp_host = sender.get("smtp_host") or MAILFORGE_SMTP_HOST
        smtp_port = int(sender.get("smtp_port") or MAILFORGE_SMTP_PORT)
        smtp_cls = smtplib.SMTP_SSL if smtp_port == 465 else smtplib.SMTP
        with smtp_cls(smtp_host, smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as server:
            server.ehlo()
            if smtp_port != 465:
                server.starttls()
                server.ehlo()
            server.login(sender_email, password)
            server.send_message(msg)
    return {"sent": True, "message_id": msg["Message-ID"]}


async def _report_existing(token, task, existing):
    event_type = "sent"
    if existing.get("status") == "scanned":
        event_type = "placement"
    elif existing.get("status") == "replied":
        event_type = "reply"
    await _report(
        token,
        task,
        event_type,
        existing.get("mailbox_email") or task.get("mailbox_email") or task.get("from_email") or "",
        message_id=existing.get("message_id", ""),
        placement=existing.get("placement", ""),
    )


async def _report(token, task, event_type, mailbox_email, message_id="", placement="", details=None):
    details = details or {}
    is_scan_report = str(task.get("task_type") or "").strip() in {"scan", "scan_placement"} or event_type == "placement"
    sender_email = task.get("to_email", "") if is_scan_report else (task.get("from_email") or mailbox_email)
    receiver_email = task.get("from_email", "") if is_scan_report else task.get("to_email", "")
    origin_task_id = task.get("parent_task_id") or task.get("task_id", "")
    payload = {
        "cluster_id": task.get("cluster_id", ""),
        "task_id": task.get("task_id", ""),
        "warm_token": task.get("warm_token", ""),
        "mailbox_email": mailbox_email,
        "sender_email": sender_email,
        "receiver_email": receiver_email,
        "origin_task_id": origin_task_id,
        "event_type": event_type,
        "message_id": message_id,
        "placement": placement,
        "details": {
            **details,
            "sender_email": sender_email,
            "receiver_email": receiver_email,
            "origin_task_id": origin_task_id,
        },
    }
    try:
        await asyncio.to_thread(report_warm_task, token, payload)
        update_warm_local_task(task.get("task_id", ""), reported=True)
    except WarmApiError as exc:
        _clear_invalid_warm_auth(exc)
        logger.warning("warm task report failed task=%s error=%s", task.get("task_id", ""), redact_sensitive(str(exc)))
