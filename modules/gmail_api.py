import base64
import os

import requests

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SCOPES = [GMAIL_SEND_SCOPE, GMAIL_READONLY_SCOPE, GMAIL_MODIFY_SCOPE]
GMAIL_BASE_SCOPES = list(GMAIL_SCOPES)
GMAIL_FULL_AUTO_WARM_SCOPES = list(GMAIL_SCOPES)
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


class GmailApiError(RuntimeError):
    def __init__(self, message, category="permanent_failure", status_code=None, payload=None):
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.payload = payload or {}


def classify_gmail_api_error(status_code=None, payload=None, message=""):
    text = f"{payload or ''} {message or ''}".lower()
    if any(item in text for item in ["invalid_grant", "token has been expired", "token has been revoked"]):
        return "auth_invalid"
    if status_code in {401, 403} and any(item in text for item in ["invalid_grant", "unauthorized", "auth", "token"]):
        return "auth_invalid"
    if status_code in {429} or any(item in text for item in ["rate limit", "ratelimit", "quota", "user-rate", "rate_limit"]):
        return "rate_limited"
    if status_code and 500 <= int(status_code) < 600:
        return "temporary_failure"
    if status_code in {408, 409, 425}:
        return "temporary_failure"
    return "permanent_failure"


def _client_config(client_id, client_secret, redirect_uri):
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }


def build_gmail_oauth_url(client_id, client_secret, redirect_uri, state, login_hint="", scopes=None):
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:  # pragma: no cover - dependency error is surfaced in UI
        raise RuntimeError("google-auth-oauthlib is not installed.") from exc

    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=scopes or GMAIL_BASE_SCOPES,
        state=state,
    )
    flow.redirect_uri = redirect_uri
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent select_account",
        login_hint=login_hint or None,
    )
    return authorization_url, flow.code_verifier


def exchange_gmail_oauth_code(client_id, client_secret, redirect_uri, authorization_response, state, code_verifier=None, scopes=None):
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("google-auth-oauthlib is not installed.") from exc

    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=scopes or GMAIL_BASE_SCOPES,
        state=state,
        code_verifier=code_verifier,
    )
    flow.redirect_uri = redirect_uri
    flow.fetch_token(authorization_response=authorization_response)
    return flow.credentials


def refresh_gmail_access_token(client_id, client_secret, refresh_token, scopes=None):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("google-auth is not installed.") from exc

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes or GMAIL_BASE_SCOPES,
    )
    try:
        credentials.refresh(Request())
    except Exception as exc:
        category = classify_gmail_api_error(message=str(exc))
        raise GmailApiError(f"Gmail API token refresh failed: {exc}", category=category) from exc
    return credentials.token


def fetch_gmail_profile(access_token):
    response = requests.get(
        GMAIL_PROFILE_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text}
    if not response.ok:
        message = payload.get("error") if isinstance(payload.get("error"), str) else payload
        category = classify_gmail_api_error(response.status_code, payload, str(message))
        raise GmailApiError(f"Gmail API profile failed: {message}", category=category, status_code=response.status_code, payload=payload)
    return payload


def send_gmail_api_message(client_id, client_secret, refresh_token, message_bytes):
    access_token = refresh_gmail_access_token(client_id, client_secret, refresh_token, scopes=[GMAIL_SEND_SCOPE])
    encoded_message = base64.urlsafe_b64encode(message_bytes).decode("ascii")
    response = requests.post(
        GMAIL_SEND_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"raw": encoded_message},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text}
    if not response.ok:
        message = payload.get("error") if isinstance(payload.get("error"), str) else payload
        category = classify_gmail_api_error(response.status_code, payload, str(message))
        raise GmailApiError(f"Gmail API send failed: {message}", category=category, status_code=response.status_code, payload=payload)
    return payload


def _decode_gmail_part_body(part):
    data = ((part or {}).get("body") or {}).get("data") or ""
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _gmail_payload_text(payload):
    if not payload:
        return ""
    chunks = [_decode_gmail_part_body(payload)]
    for part in payload.get("parts") or []:
        chunks.append(_gmail_payload_text(part))
    return "\n".join(chunk for chunk in chunks if chunk)


def _gmail_headers(payload):
    headers = {}
    for item in (payload or {}).get("headers") or []:
        name = str(item.get("name") or "").lower()
        if name:
            headers[name] = str(item.get("value") or "")
    return headers


def find_gmail_message_placement(client_id, client_secret, refresh_token, token, max_results=10):
    access_token = refresh_gmail_access_token(client_id, client_secret, refresh_token, scopes=[GMAIL_READONLY_SCOPE])
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    response = requests.get(
        GMAIL_MESSAGES_URL,
        headers=headers,
        params={"q": f'"{token}" newer_than:7d', "maxResults": max(1, min(int(max_results or 10), 25))},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text}
    if not response.ok:
        message = payload.get("error") if isinstance(payload.get("error"), str) else payload
        raise RuntimeError(f"Gmail API search failed: {message}")

    messages = payload.get("messages") or []
    if not messages:
        fallback = requests.get(
            GMAIL_MESSAGES_URL,
            headers=headers,
            params={"q": "newer_than:7d", "maxResults": max(10, min(int(max_results or 10), 25))},
            timeout=30,
        )
        try:
            fallback_payload = fallback.json()
        except ValueError:
            fallback_payload = {"error": fallback.text}
        if not fallback.ok:
            message = fallback_payload.get("error") if isinstance(fallback_payload.get("error"), str) else fallback_payload
            raise RuntimeError(f"Gmail API recent message search failed: {message}")
        messages = fallback_payload.get("messages") or []
    if not messages:
        return {"placement": "missing", "message_id": "", "labels": []}

    detail_payload = None
    message_id = ""
    for message_row in messages:
        candidate_id = message_row.get("id", "")
        if not candidate_id:
            continue
        detail = requests.get(
            f"{GMAIL_MESSAGES_URL}/{candidate_id}",
            headers=headers,
            params={"format": "full"},
            timeout=30,
        )
        try:
            candidate_payload = detail.json()
        except ValueError:
            candidate_payload = {"error": detail.text}
        if not detail.ok:
            message = candidate_payload.get("error") if isinstance(candidate_payload.get("error"), str) else candidate_payload
            raise RuntimeError(f"Gmail API message fetch failed: {message}")
        payload_text = _gmail_payload_text(candidate_payload.get("payload") or "") or candidate_payload.get("snippet", "")
        header_text = "\n".join((item.get("value") or "") for item in ((candidate_payload.get("payload") or {}).get("headers") or []))
        if token in f"{header_text}\n{payload_text}\n{candidate_payload.get('id', '')}":
            detail_payload = candidate_payload
            message_id = candidate_id
            break
    if not detail_payload:
        return {"placement": "missing", "message_id": "", "labels": []}

    labels = detail_payload.get("labelIds") or []
    payload = detail_payload.get("payload") or {}
    headers = _gmail_headers(payload)
    if "SPAM" in labels:
        placement = "spam"
    elif "INBOX" in labels:
        placement = "inbox"
    else:
        placement = "other"
    return {
        "placement": placement,
        "message_id": message_id,
        "labels": labels,
        "thread_id": detail_payload.get("threadId", ""),
        "rfc822_message_id": headers.get("message-id", ""),
        "from_email": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "references": headers.get("references", ""),
        "body": _gmail_payload_text(payload) or detail_payload.get("snippet", ""),
    }


def move_gmail_message_to_inbox(client_id, client_secret, refresh_token, message_id):
    access_token = refresh_gmail_access_token(client_id, client_secret, refresh_token, scopes=[GMAIL_MODIFY_SCOPE])
    response = requests.post(
        f"{GMAIL_MESSAGES_URL}/{message_id}/modify",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"addLabelIds": ["INBOX"], "removeLabelIds": ["SPAM"]},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text}
    if not response.ok:
        message = payload.get("error") if isinstance(payload.get("error"), str) else payload
        raise RuntimeError(f"Gmail API move to inbox failed: {message}")
    return payload
