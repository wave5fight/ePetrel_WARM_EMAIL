from urllib.parse import urlencode, urljoin

import requests

from config import EPETREL_BFF_BASE_URL
from modules.network_proxy import apply_proxy_settings


class WarmApiError(Exception):
    pass


def _bff_url(path):
    base_url = EPETREL_BFF_BASE_URL.rstrip("/") + "/"
    return urljoin(base_url, path.lstrip("/"))


def _request(method, path, token="", payload=None, timeout=15):
    apply_proxy_settings()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(method, _bff_url(path), headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise WarmApiError(str(exc)) from exc

    try:
        body = response.json()
    except ValueError:
        body = {"success": False, "error": response.text}
    if not response.ok or body.get("success") is False:
        raise WarmApiError(body.get("error") or body.get("message") or f"BFF HTTP {response.status_code}")
    return body.get("data", body)


def start_warm_auth():
    return _request("POST", "/v1/warm/auth/start")


def poll_warm_auth(device_code):
    return _request("GET", f"/v1/warm/auth/poll/{device_code}")


def register_warm_mailbox(token, payload):
    return _request("POST", "/v1/warm/mailboxes/register", token=token, payload=payload)


def create_warm_cluster(token, payload):
    return _request("POST", "/v1/warm/clusters", token=token, payload=payload)


def join_warm_cluster(token, payload):
    return _request("POST", "/v1/warm/clusters/join", token=token, payload=payload)


def fetch_warm_cluster_members(token, cluster_id, owner_payload=None):
    query = ""
    if isinstance(owner_payload, dict) and owner_payload:
        query = "?" + urlencode({
            key: value
            for key, value in owner_payload.items()
            if value is not None and str(value) != ""
        })
    return _request("GET", f"/v1/warm/clusters/{cluster_id}/members{query}", token=token)


def approve_warm_cluster_member(token, cluster_id, email, payload):
    return _request("POST", f"/v1/warm/clusters/{cluster_id}/members/{email}/approve", token=token, payload=payload)


def remove_warm_cluster_member(token, cluster_id, email, payload):
    return _request("POST", f"/v1/warm/clusters/{cluster_id}/members/{email}/remove", token=token, payload=payload)


def dissolve_warm_cluster(token, cluster_id, payload):
    return _request("POST", f"/v1/warm/clusters/{cluster_id}/dissolve", token=token, payload=payload)


def leave_warm_cluster(token, cluster_id, payload):
    return _request("POST", f"/v1/warm/clusters/{cluster_id}/leave", token=token, payload=payload)


def send_warm_account_probe(token, payload):
    return _request("POST", "/v1/warm/account-probe/send", token=token, payload=payload)


def start_warm_mailbox_ownership(token, payload):
    return _request("POST", "/v1/warm/mailboxes/ownership/start", token=token, payload=payload, timeout=30)


def verify_warm_mailbox_ownership(token, payload):
    return _request("POST", "/v1/warm/mailboxes/ownership/verify", token=token, payload=payload)


def report_warm_mailbox_ownership_reply(token, payload):
    return _request("POST", "/v1/warm/mailboxes/ownership/reply-report", token=token, payload=payload)


def list_warm_mailbox_ownership(token):
    return _request("GET", "/v1/warm/mailboxes/ownership/list", token=token)


def send_warm_heartbeat(token, payload):
    return _request("POST", "/v1/warm/heartbeat", token=token, payload=payload)


def claim_warm_tasks(token, payload):
    return _request("POST", "/v1/warm/tasks/claim", token=token, payload=payload)


def report_warm_task(token, payload):
    return _request("POST", "/v1/warm/tasks/report", token=token, payload=payload)


def fetch_warm_summary(token, cluster_id="", days=30, owner_payload=None):
    params = {
        "cluster_id": (cluster_id or "").strip(),
        "days": int(days or 30),
    }
    if isinstance(owner_payload, dict):
        params.update({
            key: value
            for key, value in owner_payload.items()
            if value is not None and str(value) != ""
        })
    query = urlencode(params)
    return _request("GET", f"/v1/warm/reports/summary?{query}", token=token)
