import time
from urllib.parse import urljoin

import requests

from config import EPETREL_BFF_BASE_URL


class EmailTestApiError(Exception):
    pass


def _bff_url(path):
    base_url = EPETREL_BFF_BASE_URL.rstrip("/") + "/"
    return urljoin(base_url, path.lstrip("/"))


def _request(method, path, token="", payload=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.request(
            method,
            _bff_url(path),
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise EmailTestApiError(str(exc)) from exc

    try:
        body = response.json()
    except ValueError:
        body = {"success": False, "error": response.text}

    if not response.ok or body.get("success") is False:
        message = body.get("error") or body.get("message") or f"BFF HTTP {response.status_code}"
        raise EmailTestApiError(message)

    return body.get("data", body)


def start_email_test_auth(return_url=""):
    payload = {"return_url": return_url} if return_url else None
    return _request("POST", "/v1/email-test/auth/start", payload=payload)


def poll_email_test_auth(device_code):
    return _request("GET", f"/v1/email-test/auth/poll/{device_code}")


def create_email_test_request(token, sender_email):
    # Deprecated: old Gmail seed placement flow. Kept for rollback only.
    return _request(
        "POST",
        "/v1/email-test/requests",
        token=token,
        payload={"sender_email": sender_email},
        timeout=45,
    )


def poll_email_test_request(token, request_id):
    # Deprecated: old Gmail seed placement flow. Kept for rollback only.
    return _request("GET", f"/v1/email-test/requests/{request_id}", token=token)


def diagnose_email_test_gmail(token, run_scan=True):
    # Deprecated: old Gmail seed placement flow. Kept for rollback only.
    return _request(
        "POST",
        "/v1/email-test/diagnostics",
        token=token,
        payload={"run_scan": bool(run_scan)},
        timeout=30,
    )


def wait_for_email_test_result(token, request_id, timeout_seconds, interval_seconds):
    deadline = time.time() + max(1, int(timeout_seconds))
    interval = max(1, int(interval_seconds))
    last_result = None

    while time.time() < deadline:
        last_result = poll_email_test_request(token, request_id)
        status = str(last_result.get("status", "")).lower()
        if status in {"completed", "failed", "expired"}:
            return last_result
        time.sleep(interval)

    return last_result or {"status": "pending", "request_id": request_id}


def analyze_email_deliverability(token, payload):
    return _request(
        "POST",
        "/v1/email-test/analyze",
        token=token,
        payload=payload,
        timeout=45,
    )


def poll_email_deliverability_analysis(token, job_id):
    return _request("GET", f"/v1/email-test/analyze/{job_id}", token=token, timeout=15)
