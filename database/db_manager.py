import base64
import json
import os
import sqlite3
from datetime import date, datetime, timezone

from config import (
    DB_PATH,
    DEFAULT_DAILY_LIMIT,
    MAIL_FROM_NAME,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    WARM_MAILBOX_OFFLINE_WARN_SEC,
    WARM_MAILBOX_STALE_SEC,
    WARM_REPLY_HARD_TIMEOUT_HOURS,
    WARM_REPLY_MIN_DELAY_HOURS,
    WARM_SCAN_HARD_TIMEOUT_HOURS,
    WARM_SCAN_SOFT_TIMEOUT_HOURS,
)

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover - keeps local development bootable
    Fernet = None


WARM_LLM_SYSTEM_PROMPT = (
    "You write plain, low-stakes mailbox warm conversation content. "
    "Your job is to make short, normal messages that sound like real people writing casual work notes or light personal check-ins. "
    "Never write sales outreach, promotions, lead generation, deliverability language, spam-filter language, or anything that reveals automation. "
    "Use simple human variety: brief business coordination, document notes, schedule checks, sports, fitness, weekend plans, holidays, congratulations, or small everyday updates. "
    "Keep subjects short, bodies concise, and replies context-aware. Output exactly what the user asks for."
)

LLM_PURPOSE_PROVIDERS = {"warm": {"warm_openai"}}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(cursor, table, column, definition):
    cursor.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cursor.fetchall()}
    if column not in columns:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def _secret_key_path():
    return os.getenv(
        "EPETREL_SECRET_KEY_PATH",
        os.path.join(os.path.dirname(DB_PATH), ".epetrel_secret.key"),
    )


def _get_cipher():
    if Fernet is None:
        return None
    path = _secret_key_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, "rb") as key_file:
            key = key_file.read().strip()
    else:
        key = Fernet.generate_key()
        with open(path, "wb") as key_file:
            key_file.write(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return Fernet(key)


def _encrypt_secret(secret):
    secret = (secret or "").strip()
    if not secret:
        return ""
    cipher = _get_cipher()
    if cipher is None:
        encoded = base64.urlsafe_b64encode(secret.encode("utf-8")).decode("ascii")
        return f"base64:{encoded}"
    return f"fernet:{cipher.encrypt(secret.encode('utf-8')).decode('ascii')}"


def _decrypt_secret(secret_cipher):
    if not secret_cipher:
        return ""
    if secret_cipher.startswith("fernet:"):
        cipher = _get_cipher()
        if cipher is None:
            return ""
        try:
            encrypted = secret_cipher.split(":", 1)[1].encode("ascii")
            return cipher.decrypt(encrypted).decode("utf-8")
        except Exception:
            return ""
    if secret_cipher.startswith("base64:"):
        try:
            encoded = secret_cipher.split(":", 1)[1].encode("ascii")
            return base64.urlsafe_b64decode(encoded).decode("utf-8")
        except Exception:
            return ""
    return secret_cipher


def _mask_secret(secret):
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}...{secret[-4:]}"


def _today():
    return date.today().isoformat()


def _llm_display_name(provider):
    return "Warm OpenAI"


def _insert_default_llm_settings(cursor):
    cursor.execute("SELECT 1 FROM llm_settings WHERE provider = 'warm_openai'")
    if cursor.fetchone():
        return
    cursor.execute(
        """
        INSERT INTO llm_settings (
            provider, display_name, api_key_cipher, base_url, model,
            system_prompt, status, updated_at
        )
        VALUES ('warm_openai', 'Warm OpenAI', ?, ?, 'gpt-4o-mini', ?, 'active', CURRENT_TIMESTAMP)
        """,
        (_encrypt_secret(OPENAI_API_KEY), OPENAI_BASE_URL, WARM_LLM_SYSTEM_PROMPT),
    )


def _refresh_default_llm_prompt(cursor):
    cursor.execute(
        """
        UPDATE llm_settings
        SET system_prompt = ?, updated_at = CURRENT_TIMESTAMP
        WHERE provider = 'warm_openai'
          AND (system_prompt IS NULL OR TRIM(system_prompt) = '')
        """,
        (WARM_LLM_SYSTEM_PROMPT,),
    )


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS senders (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL DEFAULT '',
            daily_limit INTEGER DEFAULT 40,
            daily_sent_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            from_name TEXT DEFAULT 'MutualWarm',
            last_reset_date TEXT,
            last_sent_at DATETIME,
            last_checked_at DATETIME,
            check_error TEXT DEFAULT '',
            auth_method TEXT DEFAULT 'gmail_api',
            mailbox_check_status TEXT DEFAULT 'unchecked',
            gmail_client_id TEXT DEFAULT '',
            gmail_client_secret_cipher TEXT DEFAULT '',
            gmail_refresh_token_cipher TEXT DEFAULT '',
            gmail_token_status TEXT DEFAULT 'not_connected',
            gmail_granted_scopes TEXT DEFAULT '',
            gmail_account_type TEXT DEFAULT 'workspace_gmail'
        )
        """
    )
    for column, definition in [
        ("password", "TEXT NOT NULL DEFAULT ''"),
        ("daily_sent_count", "INTEGER DEFAULT 0"),
        ("fail_count", "INTEGER DEFAULT 0"),
        ("from_name", "TEXT DEFAULT 'MutualWarm'"),
        ("last_reset_date", "TEXT"),
        ("last_sent_at", "DATETIME"),
        ("last_checked_at", "DATETIME"),
        ("check_error", "TEXT DEFAULT ''"),
        ("auth_method", "TEXT DEFAULT 'gmail_api'"),
        ("mailbox_check_status", "TEXT DEFAULT 'unchecked'"),
        ("gmail_client_id", "TEXT DEFAULT ''"),
        ("gmail_client_secret_cipher", "TEXT DEFAULT ''"),
        ("gmail_refresh_token_cipher", "TEXT DEFAULT ''"),
        ("gmail_token_status", "TEXT DEFAULT 'not_connected'"),
        ("gmail_granted_scopes", "TEXT DEFAULT ''"),
        ("gmail_account_type", "TEXT DEFAULT 'workspace_gmail'"),
    ]:
        _add_column_if_missing(cursor, "senders", column, definition)
    cursor.execute(
        """
        UPDATE senders
        SET auth_method = 'gmail_api',
            gmail_account_type = CASE
                WHEN LOWER(email) LIKE '%@gmail.com' OR LOWER(email) LIKE '%@googlemail.com' THEN 'consumer_gmail'
                ELSE COALESCE(NULLIF(gmail_account_type, ''), 'workspace_gmail')
            END
        WHERE auth_method IS NULL OR auth_method = '' OR auth_method != 'gmail_api'
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_settings (
            provider TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            api_key_cipher TEXT,
            base_url TEXT,
            model TEXT,
            system_prompt TEXT,
            status TEXT DEFAULT 'inactive',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS warm_clusters (
            cluster_id TEXT PRIMARY KEY,
            name TEXT,
            owner_email TEXT,
            owner_public_key TEXT,
            role TEXT DEFAULT 'member',
            status TEXT DEFAULT 'active',
            cluster_secret_cipher TEXT,
            owner_private_key_cipher TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS warm_cluster_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_id TEXT NOT NULL,
            email TEXT NOT NULL,
            provider TEXT,
            status TEXT DEFAULT 'pending',
            capabilities TEXT,
            daily_limit INTEGER DEFAULT 5,
            timezone TEXT,
            approved_at DATETIME,
            removed_at DATETIME,
            last_seen_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cluster_id, email)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warm_cluster_members_status ON warm_cluster_members(cluster_id, status)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS warm_mailboxes (
            email TEXT PRIMARY KEY,
            cluster_id TEXT DEFAULT '',
            provider TEXT,
            status TEXT DEFAULT 'paused',
            daily_limit INTEGER DEFAULT 5,
            timezone TEXT,
            capabilities TEXT,
            last_seen_at DATETIME,
            scan_soft_timeout_hours INTEGER DEFAULT 24,
            scan_hard_timeout_hours INTEGER DEFAULT 48,
            reply_min_delay_hours INTEGER DEFAULT 2,
            reply_hard_timeout_hours INTEGER DEFAULT 48,
            avoid_sleep_hours INTEGER DEFAULT 1,
            avoid_weekends INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for column, definition in [
        ("cluster_id", "TEXT DEFAULT ''"),
        ("scan_soft_timeout_hours", f"INTEGER DEFAULT {WARM_SCAN_SOFT_TIMEOUT_HOURS}"),
        ("scan_hard_timeout_hours", f"INTEGER DEFAULT {WARM_SCAN_HARD_TIMEOUT_HOURS}"),
        ("reply_min_delay_hours", f"INTEGER DEFAULT {WARM_REPLY_MIN_DELAY_HOURS}"),
        ("reply_hard_timeout_hours", f"INTEGER DEFAULT {WARM_REPLY_HARD_TIMEOUT_HOURS}"),
        ("avoid_sleep_hours", "INTEGER DEFAULT 1"),
        ("avoid_weekends", "INTEGER DEFAULT 1"),
    ]:
        _add_column_if_missing(cursor, "warm_mailboxes", column, definition)

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS warm_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            cluster_id TEXT DEFAULT '',
            mailbox_email TEXT,
            task_id TEXT,
            event_type TEXT,
            status TEXT,
            placement TEXT,
            message_id TEXT,
            details TEXT
        )
        """
    )
    _add_column_if_missing(cursor, "warm_events", "cluster_id", "TEXT DEFAULT ''")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warm_events_mailbox_time ON warm_events(mailbox_email, event_time)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warm_events_type_time ON warm_events(event_type, event_time)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS warm_local_tasks (
            task_id TEXT PRIMARY KEY,
            cluster_id TEXT DEFAULT '',
            task_type TEXT DEFAULT '',
            mailbox_email TEXT DEFAULT '',
            peer_email TEXT DEFAULT '',
            payload_json TEXT DEFAULT '',
            status TEXT DEFAULT 'claimed',
            message_id TEXT DEFAULT '',
            placement TEXT DEFAULT '',
            error TEXT DEFAULT '',
            claimed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            reported_at DATETIME,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warm_local_tasks_status ON warm_local_tasks(status, updated_at)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS warm_worker_state (
            cluster_id TEXT NOT NULL DEFAULT '',
            mailbox_email TEXT NOT NULL DEFAULT '',
            status TEXT DEFAULT '',
            scheduler TEXT DEFAULT '',
            claim_message TEXT DEFAULT '',
            tasks_claimed INTEGER DEFAULT 0,
            completed_count INTEGER DEFAULT 0,
            last_heartbeat_at DATETIME,
            last_claim_at DATETIME,
            last_success_at DATETIME,
            last_error_at DATETIME,
            last_error TEXT DEFAULT '',
            details_json TEXT DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (cluster_id, mailbox_email)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warm_worker_state_updated ON warm_worker_state(updated_at)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS warm_local_threads (
            thread_id TEXT PRIMARY KEY,
            cluster_id TEXT DEFAULT '',
            sender_email TEXT DEFAULT '',
            peer_email TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            last_message_id TEXT DEFAULT '',
            provider_thread_id TEXT DEFAULT '',
            topic TEXT DEFAULT '',
            persona TEXT DEFAULT '',
            context_json TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warm_local_threads_pair ON warm_local_threads(cluster_id, sender_email, peer_email)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS warm_content_fingerprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_id TEXT DEFAULT '',
            task_id TEXT DEFAULT '',
            sender_email TEXT DEFAULT '',
            receiver_email TEXT DEFAULT '',
            topic TEXT DEFAULT '',
            persona TEXT DEFAULT '',
            subject_hash TEXT DEFAULT '',
            body_hash TEXT DEFAULT '',
            simhash TEXT DEFAULT '',
            recipe_hash TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warm_content_hashes ON warm_content_fingerprints(subject_hash, body_hash, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_warm_content_pair_time ON warm_content_fingerprints(cluster_id, sender_email, receiver_email, created_at)")

    _insert_default_llm_settings(cursor)
    _refresh_default_llm_prompt(cursor)
    conn.commit()
    conn.close()


def reset_daily_counters_if_needed(cursor):
    today = _today()
    cursor.execute(
        """
        UPDATE senders
        SET daily_sent_count = 0, last_reset_date = ?
        WHERE last_reset_date IS NULL OR last_reset_date != ?
        """,
        (today, today),
    )


def upsert_sender(
    email,
    password="",
    daily_limit=DEFAULT_DAILY_LIMIT,
    status="active",
    from_name=None,
    mailbox_check_status="unchecked",
    check_error="",
    auth_method="gmail_api",
    gmail_client_id=None,
    gmail_client_secret=None,
    gmail_refresh_token=None,
    gmail_token_status=None,
    gmail_granted_scopes=None,
    gmail_account_type=None,
):
    clean_email = (email or "").strip().lower()
    if not clean_email:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM senders WHERE email = ?", (clean_email,))
    existing = dict(cursor.fetchone() or {})

    def secret_cipher(value, column):
        if value is None:
            return existing.get(column, "")
        return _encrypt_secret(value)

    cursor.execute(
        """
        INSERT INTO senders (
            email, password, daily_limit, status, from_name, last_reset_date,
            last_checked_at, check_error, auth_method, mailbox_check_status,
            gmail_client_id, gmail_client_secret_cipher, gmail_refresh_token_cipher,
            gmail_token_status, gmail_granted_scopes, gmail_account_type
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            password = excluded.password,
            daily_limit = excluded.daily_limit,
            status = excluded.status,
            from_name = excluded.from_name,
            last_checked_at = excluded.last_checked_at,
            check_error = excluded.check_error,
            auth_method = excluded.auth_method,
            mailbox_check_status = excluded.mailbox_check_status,
            gmail_client_id = excluded.gmail_client_id,
            gmail_client_secret_cipher = excluded.gmail_client_secret_cipher,
            gmail_refresh_token_cipher = excluded.gmail_refresh_token_cipher,
            gmail_token_status = excluded.gmail_token_status,
            gmail_granted_scopes = excluded.gmail_granted_scopes,
            gmail_account_type = excluded.gmail_account_type
        """,
        (
            clean_email,
            password or existing.get("password", "") or "",
            int(daily_limit or DEFAULT_DAILY_LIMIT),
            status or existing.get("status") or "active",
            from_name or existing.get("from_name") or MAIL_FROM_NAME,
            _today(),
            check_error or "",
            "gmail_api",
            mailbox_check_status or existing.get("mailbox_check_status") or "unchecked",
            gmail_client_id if gmail_client_id is not None else existing.get("gmail_client_id", ""),
            secret_cipher(gmail_client_secret, "gmail_client_secret_cipher"),
            secret_cipher(gmail_refresh_token, "gmail_refresh_token_cipher"),
            gmail_token_status if gmail_token_status is not None else existing.get("gmail_token_status", "not_connected"),
            gmail_granted_scopes if gmail_granted_scopes is not None else existing.get("gmail_granted_scopes", ""),
            gmail_account_type if gmail_account_type is not None else existing.get("gmail_account_type", "workspace_gmail"),
        ),
    )
    conn.commit()
    conn.close()
    return True


def _sender_dict(row, include_credentials=False):
    data = dict(row)
    secret = _decrypt_secret(data.pop("gmail_client_secret_cipher", ""))
    refresh = _decrypt_secret(data.pop("gmail_refresh_token_cipher", ""))
    data["auth_method"] = "gmail_api"
    data["gmail_client_secret"] = secret if include_credentials else ""
    data["gmail_refresh_token"] = refresh if include_credentials else ""
    data["has_gmail_client_secret"] = bool(secret)
    data["has_gmail_refresh_token"] = bool(refresh)
    return data


def list_senders(include_credentials=False):
    conn = get_connection()
    cursor = conn.cursor()
    reset_daily_counters_if_needed(cursor)
    cursor.execute(
        """
        SELECT email, password, daily_limit, daily_sent_count, fail_count, status,
               from_name, last_checked_at, check_error, auth_method, mailbox_check_status,
               gmail_client_id, gmail_client_secret_cipher, gmail_refresh_token_cipher,
               gmail_token_status, gmail_granted_scopes, gmail_account_type
        FROM senders
        ORDER BY status, email
        """
    )
    rows = [_sender_dict(row, include_credentials=include_credentials) for row in cursor.fetchall()]
    conn.commit()
    conn.close()
    return rows


def get_sender(email):
    conn = get_connection()
    cursor = conn.cursor()
    reset_daily_counters_if_needed(cursor)
    cursor.execute("SELECT * FROM senders WHERE email = ?", ((email or "").strip().lower(),))
    row = cursor.fetchone()
    conn.commit()
    conn.close()
    if not row:
        return None
    return _sender_dict(row, include_credentials=True)


def delete_sender(email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM senders WHERE email = ?", ((email or "").strip().lower(),))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def clear_senders():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM senders")
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def get_app_setting(key, default=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row is not None else default


def upsert_app_setting(key, value):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value),
    )
    conn.commit()
    conn.close()


def get_secret_app_setting(key, default=None):
    value = get_app_setting(key, None)
    if value is None:
        return default
    decrypted = _decrypt_secret(value)
    return decrypted if decrypted else default


def upsert_secret_app_setting(key, value):
    upsert_app_setting(key, _encrypt_secret(value))


def list_llm_settings(include_secrets=False, purpose="warm"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT provider, display_name, api_key_cipher, base_url, model,
               system_prompt, status, updated_at
        FROM llm_settings
        WHERE provider = 'warm_openai'
        ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, provider
        """
    )
    rows = []
    for row in cursor.fetchall():
        data = dict(row)
        api_key = _decrypt_secret(data.pop("api_key_cipher", ""))
        data["has_api_key"] = bool(api_key)
        data["api_key_preview"] = _mask_secret(api_key)
        if include_secrets:
            data["api_key"] = api_key
        rows.append(data)
    conn.close()
    return rows


def get_llm_settings(provider=None, purpose="warm"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT provider, display_name, api_key_cipher, base_url, model,
               system_prompt, status, updated_at
        FROM llm_settings
        WHERE provider = 'warm_openai'
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    data = dict(row)
    data["api_key"] = _decrypt_secret(data.pop("api_key_cipher", ""))
    data["has_api_key"] = bool(data["api_key"])
    data["api_key_preview"] = _mask_secret(data["api_key"])
    return data


def upsert_llm_settings(provider, api_key=None, base_url="", model="", system_prompt="", status="active"):
    provider = "warm_openai"
    existing = get_llm_settings(provider)
    api_key_cipher = None if api_key is None or api_key == "" else _encrypt_secret(api_key)
    conn = get_connection()
    cursor = conn.cursor()
    if existing:
        values = {
            "base_url": base_url,
            "model": model,
            "system_prompt": system_prompt,
            "status": status,
            "provider": provider,
        }
        if api_key_cipher is None:
            cursor.execute(
                """
                UPDATE llm_settings
                SET display_name = 'Warm OpenAI',
                    base_url = :base_url,
                    model = :model,
                    system_prompt = :system_prompt,
                    status = :status,
                    updated_at = CURRENT_TIMESTAMP
                WHERE provider = :provider
                """,
                values,
            )
        else:
            values["api_key_cipher"] = api_key_cipher
            cursor.execute(
                """
                UPDATE llm_settings
                SET display_name = 'Warm OpenAI',
                    api_key_cipher = :api_key_cipher,
                    base_url = :base_url,
                    model = :model,
                    system_prompt = :system_prompt,
                    status = :status,
                    updated_at = CURRENT_TIMESTAMP
                WHERE provider = :provider
                """,
                values,
            )
    else:
        cursor.execute(
            """
            INSERT INTO llm_settings (
                provider, display_name, api_key_cipher, base_url, model,
                system_prompt, status, updated_at
            )
            VALUES ('warm_openai', 'Warm OpenAI', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (api_key_cipher or "", base_url, model, system_prompt, status),
        )
    conn.commit()
    conn.close()


def upsert_warm_mailbox(
    email,
    cluster_id="",
    provider="",
    status="active",
    daily_limit=5,
    timezone="",
    capabilities="send,scan,reply",
    scan_soft_timeout_hours=WARM_SCAN_SOFT_TIMEOUT_HOURS,
    scan_hard_timeout_hours=WARM_SCAN_HARD_TIMEOUT_HOURS,
    reply_min_delay_hours=WARM_REPLY_MIN_DELAY_HOURS,
    reply_hard_timeout_hours=WARM_REPLY_HARD_TIMEOUT_HOURS,
    avoid_sleep_hours=True,
    avoid_weekends=True,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO warm_mailboxes (
            email, cluster_id, provider, status, daily_limit, timezone, capabilities,
            last_seen_at, scan_soft_timeout_hours, scan_hard_timeout_hours,
            reply_min_delay_hours, reply_hard_timeout_hours, avoid_sleep_hours,
            avoid_weekends, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(email) DO UPDATE SET
            cluster_id = excluded.cluster_id,
            provider = excluded.provider,
            status = excluded.status,
            daily_limit = excluded.daily_limit,
            timezone = excluded.timezone,
            capabilities = excluded.capabilities,
            last_seen_at = excluded.last_seen_at,
            scan_soft_timeout_hours = excluded.scan_soft_timeout_hours,
            scan_hard_timeout_hours = excluded.scan_hard_timeout_hours,
            reply_min_delay_hours = excluded.reply_min_delay_hours,
            reply_hard_timeout_hours = excluded.reply_hard_timeout_hours,
            avoid_sleep_hours = excluded.avoid_sleep_hours,
            avoid_weekends = excluded.avoid_weekends,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            (email or "").strip().lower(),
            cluster_id,
            provider,
            status,
            int(daily_limit or 5),
            timezone,
            capabilities,
            int(scan_soft_timeout_hours or WARM_SCAN_SOFT_TIMEOUT_HOURS),
            int(scan_hard_timeout_hours or WARM_SCAN_HARD_TIMEOUT_HOURS),
            int(reply_min_delay_hours or WARM_REPLY_MIN_DELAY_HOURS),
            int(reply_hard_timeout_hours or WARM_REPLY_HARD_TIMEOUT_HOURS),
            1 if avoid_sleep_hours else 0,
            1 if avoid_weekends else 0,
        ),
    )
    conn.commit()
    conn.close()


def list_warm_mailboxes():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT email, cluster_id, provider, status, daily_limit, timezone, capabilities,
               last_seen_at, scan_soft_timeout_hours, scan_hard_timeout_hours,
               reply_min_delay_hours, reply_hard_timeout_hours, avoid_sleep_hours,
               avoid_weekends, created_at, updated_at
        FROM warm_mailboxes
        ORDER BY status, email
        """
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def update_warm_mailbox_status(email, status):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE warm_mailboxes
        SET status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE email = ?
        """,
        (status, (email or "").strip().lower()),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed


def delete_warm_mailbox(email, cluster_id=""):
    conn = get_connection()
    cursor = conn.cursor()
    clean_email = (email or "").strip().lower()
    if cluster_id:
        cursor.execute("DELETE FROM warm_mailboxes WHERE email = ? AND cluster_id = ?", (clean_email, (cluster_id or "").strip()))
    else:
        cursor.execute("DELETE FROM warm_mailboxes WHERE email = ?", (clean_email,))
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed


def clear_warm_mailboxes():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM warm_mailboxes")
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def log_warm_event(cluster_id="", mailbox_email="", task_id="", event_type="", status="", placement="", message_id="", details=""):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO warm_events (
            cluster_id, mailbox_email, task_id, event_type, status, placement, message_id, details
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cluster_id,
            (mailbox_email or "").strip().lower(),
            task_id,
            event_type,
            status,
            placement,
            message_id,
            details,
        ),
    )
    conn.commit()
    conn.close()


def _warm_summary_rate(numerator, denominator):
    return numerator / denominator if denominator else 0


def _warm_parse_datetime(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _warm_mailbox_health(last_seen_at):
    seen = _warm_parse_datetime(last_seen_at)
    if not seen:
        return {
            "seconds_since_seen": None,
            "health_status": "never_seen",
            "health_reason": "No successful heartbeat has been recorded for this mailbox.",
            "last_seen_at": last_seen_at or "",
        }
    age = max(0, int((datetime.now(timezone.utc) - seen).total_seconds()))
    warn_sec = max(60, int(WARM_MAILBOX_OFFLINE_WARN_SEC or 3600))
    stale_sec = max(warn_sec, int(WARM_MAILBOX_STALE_SEC or 259200))
    if age >= stale_sec:
        status = "stale_lost_task_risk"
        reason = "No heartbeat for 72h+; queued Redis tasks may have expired."
    elif age >= warn_sec:
        status = "offline_warning"
        reason = "No heartbeat for 1h+."
    else:
        status = "online"
        reason = "Recent heartbeat received."
    return {
        "seconds_since_seen": age,
        "health_status": status,
        "health_reason": reason,
        "last_seen_at": last_seen_at or "",
    }


def get_warm_summary(days=30, cluster_id=""):
    conn = get_connection()
    cursor = conn.cursor()
    days = max(1, int(days or 30))
    params = []
    mailbox_where = "WHERE status = 'active'"
    if cluster_id:
        mailbox_where += " AND cluster_id = ?"
        params.append((cluster_id or "").strip())
    cursor.execute(f"SELECT COUNT(*) AS count FROM warm_mailboxes {mailbox_where}", params)
    active_mailboxes = int((cursor.fetchone() or {"count": 0})["count"] or 0)
    event_params = [f"-{days} days"]
    event_where = "datetime(event_time) >= datetime('now', ?)"
    if cluster_id:
        event_where += " AND cluster_id = ?"
        event_params.append((cluster_id or "").strip())
    cursor.execute(
        f"""
        SELECT
            SUM(CASE WHEN event_type = 'sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN event_type = 'reply' THEN 1 ELSE 0 END) AS reply_count,
            SUM(CASE WHEN event_type = 'placement' THEN 1 ELSE 0 END) AS placement_count,
            SUM(CASE WHEN placement = 'inbox' THEN 1 ELSE 0 END) AS inbox_count,
            SUM(CASE WHEN placement = 'spam' THEN 1 ELSE 0 END) AS spam_count,
            SUM(CASE WHEN placement = 'other' THEN 1 ELSE 0 END) AS other_count,
            SUM(CASE WHEN placement = 'missing' THEN 1 ELSE 0 END) AS missing_count,
            MAX(event_time) AS last_event_at
        FROM warm_events
        WHERE {event_where}
        """,
        event_params,
    )
    row = dict(cursor.fetchone() or {})
    cursor.execute(
        f"""
        SELECT
            COALESCE(NULLIF(mailbox_email, ''), '(unknown)') AS email,
            SUM(CASE WHEN event_type = 'sent' THEN 1 ELSE 0 END) AS sent_count,
            SUM(CASE WHEN event_type = 'reply' THEN 1 ELSE 0 END) AS reply_count,
            SUM(CASE WHEN event_type = 'placement' THEN 1 ELSE 0 END) AS placement_count,
            SUM(CASE WHEN placement = 'inbox' THEN 1 ELSE 0 END) AS inbox_count,
            SUM(CASE WHEN placement = 'spam' THEN 1 ELSE 0 END) AS spam_count,
            SUM(CASE WHEN placement = 'other' THEN 1 ELSE 0 END) AS other_count,
            SUM(CASE WHEN placement = 'missing' THEN 1 ELSE 0 END) AS missing_count,
            MAX(event_time) AS last_event_at
        FROM warm_events
        WHERE {event_where}
        GROUP BY COALESCE(NULLIF(mailbox_email, ''), '(unknown)')
        ORDER BY last_event_at DESC, email
        """,
        event_params,
    )
    event_rows = [dict(item) for item in cursor.fetchall()]
    state_params = []
    state_where = "1 = 1"
    if cluster_id:
        state_where += " AND cluster_id = ?"
        state_params.append((cluster_id or "").strip())
    cursor.execute(
        f"""
        SELECT *
        FROM warm_worker_state
        WHERE {state_where}
        ORDER BY updated_at DESC, mailbox_email
        """,
        state_params,
    )
    state_rows = {
        (item["mailbox_email"] or "").strip().lower(): dict(item)
        for item in cursor.fetchall()
    }
    cursor.execute(f"SELECT email, status FROM warm_mailboxes {mailbox_where}", params)
    active_rows = [
        {
            "email": (item["email"] or "").strip().lower(),
            "status": item["status"] or "active",
        }
        for item in cursor.fetchall()
    ]
    conn.close()
    mailbox_rows = []
    known_emails = set()
    for item in event_rows:
        email = (item.get("email") or "").strip().lower()
        known_emails.add(email)
        placement_count = int(item.get("placement_count") or 0)
        inbox_count = int(item.get("inbox_count") or 0)
        spam_count = int(item.get("spam_count") or 0)
        state = state_rows.get(email, {})
        health = _warm_mailbox_health(state.get("last_heartbeat_at", ""))
        mailbox_rows.append({
            "email": email,
            "sent_count": int(item.get("sent_count") or 0),
            "reply_count": int(item.get("reply_count") or 0),
            "received_count": placement_count,
            "placement_count": placement_count,
            "inbox_count": inbox_count,
            "spam_count": spam_count,
            "other_count": int(item.get("other_count") or 0),
            "missing_count": int(item.get("missing_count") or 0),
            "inbox_rate": _warm_summary_rate(inbox_count, placement_count),
            "spam_rate": _warm_summary_rate(spam_count, placement_count),
            "last_event_at": item.get("last_event_at") or "",
            "worker_status": state.get("status", ""),
            "claim_message": state.get("claim_message", ""),
            "scheduler": state.get("scheduler", ""),
            "last_claim_at": state.get("last_claim_at", ""),
            "last_heartbeat_at": state.get("last_heartbeat_at", ""),
            "last_error": state.get("last_error", ""),
            **health,
        })
    for item in active_rows:
        email = (item.get("email") or "").strip().lower()
        if not email or email in known_emails:
            continue
        known_emails.add(email)
        state = state_rows.get(email, {})
        health = _warm_mailbox_health(state.get("last_heartbeat_at", ""))
        mailbox_rows.append({
            "email": email,
            "sent_count": 0,
            "reply_count": 0,
            "received_count": 0,
            "placement_count": 0,
            "inbox_count": 0,
            "spam_count": 0,
            "other_count": 0,
            "missing_count": 0,
            "inbox_rate": 0,
            "spam_rate": 0,
            "last_event_at": "",
            "worker_status": state.get("status", "") or item.get("status", ""),
            "claim_message": state.get("claim_message", ""),
            "scheduler": state.get("scheduler", ""),
            "last_claim_at": state.get("last_claim_at", ""),
            "last_heartbeat_at": state.get("last_heartbeat_at", ""),
            "last_error": state.get("last_error", ""),
            **health,
        })
    for email, state in state_rows.items():
        if email in known_emails:
            continue
        health = _warm_mailbox_health(state.get("last_heartbeat_at", ""))
        mailbox_rows.append({
            "email": email,
            "sent_count": 0,
            "reply_count": 0,
            "received_count": 0,
            "placement_count": 0,
            "inbox_count": 0,
            "spam_count": 0,
            "other_count": 0,
            "missing_count": 0,
            "inbox_rate": 0,
            "spam_rate": 0,
            "last_event_at": "",
            "worker_status": state.get("status", ""),
            "claim_message": state.get("claim_message", ""),
            "scheduler": state.get("scheduler", ""),
            "last_claim_at": state.get("last_claim_at", ""),
            "last_heartbeat_at": state.get("last_heartbeat_at", ""),
            "last_error": state.get("last_error", ""),
            **health,
        })
    placement_count = int(row.get("placement_count") or 0)
    sent_count = int(row.get("sent_count") or 0)
    reply_count = int(row.get("reply_count") or 0)
    inbox_count = int(row.get("inbox_count") or 0)
    spam_count = int(row.get("spam_count") or 0)
    return {
        "scope": "local",
        "days": days,
        "cluster_id": (cluster_id or "").strip(),
        "active_mailboxes": active_mailboxes,
        "sent_count": sent_count,
        "reply_count": reply_count,
        "sent_total": sent_count + reply_count,
        "received_count": placement_count,
        "placement_count": placement_count,
        "inbox_count": inbox_count,
        "spam_count": spam_count,
        "other_count": int(row.get("other_count") or 0),
        "missing_count": int(row.get("missing_count") or 0),
        "inbox_rate": _warm_summary_rate(inbox_count, placement_count),
        "spam_rate": _warm_summary_rate(spam_count, placement_count),
        "last_event_at": row.get("last_event_at") or "",
        "mailbox_rows": mailbox_rows,
    }


def upsert_warm_worker_state(
    cluster_id="",
    mailbox_email="",
    status="",
    scheduler="",
    claim_message="",
    tasks_claimed=0,
    completed_count=0,
    error="",
    heartbeat=False,
    claimed=False,
    success=False,
    details=None,
):
    conn = get_connection()
    cursor = conn.cursor()
    clean_cluster = (cluster_id or "").strip()
    clean_email = (mailbox_email or "").strip().lower()
    if not clean_cluster and not clean_email:
        conn.close()
        return False
    cursor.execute(
        """
        INSERT INTO warm_worker_state (
            cluster_id, mailbox_email, status, scheduler, claim_message, tasks_claimed,
            completed_count, last_heartbeat_at, last_claim_at, last_success_at,
            last_error_at, last_error, details_json, updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
            CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
            CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
            CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
            ?, ?, CURRENT_TIMESTAMP
        )
        ON CONFLICT(cluster_id, mailbox_email) DO UPDATE SET
            status = COALESCE(NULLIF(excluded.status, ''), warm_worker_state.status),
            scheduler = COALESCE(NULLIF(excluded.scheduler, ''), warm_worker_state.scheduler),
            claim_message = CASE
                WHEN excluded.last_claim_at IS NOT NULL THEN excluded.claim_message
                WHEN excluded.status IN ('heartbeat_failed', 'heartbeat_ok') THEN ''
                ELSE COALESCE(NULLIF(excluded.claim_message, ''), warm_worker_state.claim_message)
            END,
            tasks_claimed = excluded.tasks_claimed,
            completed_count = warm_worker_state.completed_count + excluded.completed_count,
            last_heartbeat_at = COALESCE(excluded.last_heartbeat_at, warm_worker_state.last_heartbeat_at),
            last_claim_at = COALESCE(excluded.last_claim_at, warm_worker_state.last_claim_at),
            last_success_at = COALESCE(excluded.last_success_at, warm_worker_state.last_success_at),
            last_error_at = COALESCE(excluded.last_error_at, warm_worker_state.last_error_at),
            last_error = CASE
                WHEN excluded.last_error <> '' THEN excluded.last_error
                WHEN excluded.last_success_at IS NOT NULL OR excluded.status IN ('heartbeat_ok', 'claimed', 'claim_empty') THEN ''
                ELSE warm_worker_state.last_error
            END,
            details_json = COALESCE(NULLIF(excluded.details_json, ''), warm_worker_state.details_json),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            clean_cluster,
            clean_email,
            status,
            scheduler,
            claim_message,
            int(tasks_claimed or 0),
            int(completed_count or 0),
            1 if heartbeat else 0,
            1 if claimed else 0,
            1 if success else 0,
            1 if error else 0,
            str(error or ""),
            json_dumps(details or {}) if details else "",
        ),
    )
    conn.commit()
    conn.close()
    return True


def upsert_warm_local_task(task_id, cluster_id="", task_type="", mailbox_email="", peer_email="", payload=None, status="claimed"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO warm_local_tasks (
            task_id, cluster_id, task_type, mailbox_email, peer_email, payload_json,
            status, claimed_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(task_id) DO UPDATE SET
            cluster_id = COALESCE(NULLIF(excluded.cluster_id, ''), warm_local_tasks.cluster_id),
            task_type = COALESCE(NULLIF(excluded.task_type, ''), warm_local_tasks.task_type),
            mailbox_email = COALESCE(NULLIF(excluded.mailbox_email, ''), warm_local_tasks.mailbox_email),
            peer_email = COALESCE(NULLIF(excluded.peer_email, ''), warm_local_tasks.peer_email),
            payload_json = COALESCE(NULLIF(excluded.payload_json, ''), warm_local_tasks.payload_json),
            status = CASE
                WHEN warm_local_tasks.status IN ('sent', 'scanned', 'replied', 'reported') THEN warm_local_tasks.status
                ELSE excluded.status
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            task_id,
            cluster_id,
            (task_type or "").strip(),
            (mailbox_email or "").strip().lower(),
            (peer_email or "").strip().lower(),
            json_dumps(payload or {}),
            status,
        ),
    )
    conn.commit()
    conn.close()


def update_warm_local_task(task_id, status="", message_id="", placement="", error="", reported=False):
    conn = get_connection()
    cursor = conn.cursor()
    updates = ["updated_at = CURRENT_TIMESTAMP"]
    params = []
    if status:
        updates.append("status = ?")
        params.append(status)
        if status in {"sent", "scanned", "replied", "failed"}:
            updates.append("completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)")
    if message_id:
        updates.append("message_id = ?")
        params.append(message_id)
    if placement:
        updates.append("placement = ?")
        params.append(placement)
    if error:
        updates.append("error = ?")
        params.append(error)
    if reported:
        updates.append("reported_at = CURRENT_TIMESTAMP")
    params.append(task_id)
    cursor.execute(f"UPDATE warm_local_tasks SET {', '.join(updates)} WHERE task_id = ?", params)
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed


def get_warm_local_task(task_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM warm_local_tasks WHERE task_id = ?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {}


def upsert_warm_local_thread(
    thread_id,
    cluster_id="",
    sender_email="",
    peer_email="",
    subject="",
    last_message_id="",
    provider_thread_id="",
    topic="",
    persona="",
    context=None,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO warm_local_threads (
            thread_id, cluster_id, sender_email, peer_email, subject, last_message_id,
            provider_thread_id, topic, persona, context_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(thread_id) DO UPDATE SET
            subject = COALESCE(NULLIF(excluded.subject, ''), warm_local_threads.subject),
            last_message_id = COALESCE(NULLIF(excluded.last_message_id, ''), warm_local_threads.last_message_id),
            provider_thread_id = COALESCE(NULLIF(excluded.provider_thread_id, ''), warm_local_threads.provider_thread_id),
            topic = COALESCE(NULLIF(excluded.topic, ''), warm_local_threads.topic),
            persona = COALESCE(NULLIF(excluded.persona, ''), warm_local_threads.persona),
            context_json = COALESCE(NULLIF(excluded.context_json, ''), warm_local_threads.context_json),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            thread_id,
            cluster_id,
            (sender_email or "").strip().lower(),
            (peer_email or "").strip().lower(),
            subject,
            last_message_id,
            provider_thread_id,
            topic,
            persona,
            json_dumps(context or {}),
        ),
    )
    conn.commit()
    conn.close()


def list_warm_content_fingerprints(cluster_id="", sender_email="", receiver_email="", days=30):
    conn = get_connection()
    cursor = conn.cursor()
    clauses = ["datetime(created_at) >= datetime('now', ?)"]
    params = [f"-{max(1, int(days or 30))} days"]
    if cluster_id:
        clauses.append("cluster_id = ?")
        params.append(cluster_id)
    if sender_email:
        clauses.append("sender_email = ?")
        params.append((sender_email or "").strip().lower())
    if receiver_email:
        clauses.append("receiver_email = ?")
        params.append((receiver_email or "").strip().lower())
    cursor.execute(
        f"""
        SELECT *
        FROM warm_content_fingerprints
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC
        LIMIT 500
        """,
        params,
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def insert_warm_content_fingerprint(
    cluster_id="",
    task_id="",
    sender_email="",
    receiver_email="",
    topic="",
    persona="",
    subject_hash="",
    body_hash="",
    simhash="",
    recipe_hash="",
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO warm_content_fingerprints (
            cluster_id, task_id, sender_email, receiver_email, topic, persona,
            subject_hash, body_hash, simhash, recipe_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cluster_id,
            task_id,
            (sender_email or "").strip().lower(),
            (receiver_email or "").strip().lower(),
            topic,
            persona,
            subject_hash,
            body_hash,
            simhash,
            recipe_hash,
        ),
    )
    conn.commit()
    conn.close()


def json_dumps(value):
    import json

    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def upsert_warm_cluster(
    cluster_id,
    name="",
    owner_email="",
    owner_public_key="",
    role="member",
    status="active",
    cluster_secret="",
    owner_private_key="",
):
    cluster_id = (cluster_id or "").strip()
    if not cluster_id:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    existing = None
    cursor.execute("SELECT cluster_secret_cipher, owner_private_key_cipher FROM warm_clusters WHERE cluster_id = ?", (cluster_id,))
    row = cursor.fetchone()
    if row:
        existing = dict(row)
    secret_cipher = _encrypt_secret(cluster_secret) if cluster_secret else (existing or {}).get("cluster_secret_cipher", "")
    private_cipher = _encrypt_secret(owner_private_key) if owner_private_key else (existing or {}).get("owner_private_key_cipher", "")
    cursor.execute(
        """
        INSERT INTO warm_clusters (
            cluster_id, name, owner_email, owner_public_key, role, status,
            cluster_secret_cipher, owner_private_key_cipher, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(cluster_id) DO UPDATE SET
            name = excluded.name,
            owner_email = excluded.owner_email,
            owner_public_key = excluded.owner_public_key,
            role = excluded.role,
            status = excluded.status,
            cluster_secret_cipher = excluded.cluster_secret_cipher,
            owner_private_key_cipher = excluded.owner_private_key_cipher,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            cluster_id,
            name,
            (owner_email or "").strip().lower(),
            owner_public_key,
            role if role in {"owner", "member"} else "member",
            status if status in {"active", "paused", "pending", "dissolved"} else "active",
            secret_cipher,
            private_cipher,
        ),
    )
    conn.commit()
    conn.close()
    return True


def list_warm_clusters(include_secrets=False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT cluster_id, name, owner_email, owner_public_key, role, status,
               cluster_secret_cipher, owner_private_key_cipher, created_at, updated_at
        FROM warm_clusters
        ORDER BY updated_at DESC, name
        """
    )
    rows = []
    for row in cursor.fetchall():
        data = dict(row)
        secret = _decrypt_secret(data.pop("cluster_secret_cipher", ""))
        private_key = _decrypt_secret(data.pop("owner_private_key_cipher", ""))
        data["cluster_secret"] = secret if include_secrets else ""
        data["cluster_secret_masked"] = _mask_secret(secret)
        data["owner_private_key"] = private_key if include_secrets else ""
        rows.append(data)
    conn.close()
    return rows


def get_warm_cluster(cluster_id, include_secrets=False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT cluster_id, name, owner_email, owner_public_key, role, status,
               cluster_secret_cipher, owner_private_key_cipher, created_at, updated_at
        FROM warm_clusters
        WHERE cluster_id = ?
        """,
        ((cluster_id or "").strip(),),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {}
    data = dict(row)
    secret = _decrypt_secret(data.pop("cluster_secret_cipher", ""))
    private_key = _decrypt_secret(data.pop("owner_private_key_cipher", ""))
    data["cluster_secret"] = secret if include_secrets else ""
    data["cluster_secret_masked"] = _mask_secret(secret)
    data["owner_private_key"] = private_key if include_secrets else ""
    return data


def keep_only_warm_cluster(cluster_id):
    cluster_id = (cluster_id or "").strip()
    if not cluster_id:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM warm_cluster_members WHERE cluster_id != ?", (cluster_id,))
    cursor.execute("DELETE FROM warm_mailboxes WHERE cluster_id != ?", (cluster_id,))
    cursor.execute("DELETE FROM warm_clusters WHERE cluster_id != ?", (cluster_id,))
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed


def clear_warm_cluster_state():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM warm_cluster_members")
    member_deleted = cursor.rowcount
    cursor.execute("DELETE FROM warm_mailboxes")
    mailbox_deleted = cursor.rowcount
    cursor.execute("DELETE FROM warm_clusters")
    cluster_deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return cluster_deleted + member_deleted + mailbox_deleted


def mark_warm_cluster_dissolved(cluster_id):
    cluster_id = (cluster_id or "").strip()
    if not cluster_id:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE warm_clusters SET status = 'dissolved', updated_at = CURRENT_TIMESTAMP WHERE cluster_id = ?",
        (cluster_id,),
    )
    changed = cursor.rowcount
    cursor.execute(
        "UPDATE warm_mailboxes SET status = 'paused', updated_at = CURRENT_TIMESTAMP WHERE cluster_id = ?",
        (cluster_id,),
    )
    changed += cursor.rowcount
    cursor.execute(
        "UPDATE warm_cluster_members SET status = 'paused', updated_at = CURRENT_TIMESTAMP WHERE cluster_id = ?",
        (cluster_id,),
    )
    changed += cursor.rowcount
    conn.commit()
    conn.close()
    return changed


def upsert_warm_cluster_member(
    cluster_id,
    email,
    provider="",
    status="pending",
    capabilities="send,scan,reply",
    daily_limit=5,
    timezone="",
):
    conn = get_connection()
    cursor = conn.cursor()
    clean_email = (email or "").strip().lower()
    next_status = status if status in {"pending", "active", "paused", "blacklisted"} else "pending"
    approved_expr = "CURRENT_TIMESTAMP" if next_status == "active" else "approved_at"
    removed_expr = "CURRENT_TIMESTAMP" if next_status == "blacklisted" else "removed_at"
    cursor.execute(
        f"""
        INSERT INTO warm_cluster_members (
            cluster_id, email, provider, status, capabilities, daily_limit,
            timezone, approved_at, removed_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, {('CURRENT_TIMESTAMP' if next_status == 'active' else 'NULL')}, {('CURRENT_TIMESTAMP' if next_status == 'blacklisted' else 'NULL')}, CURRENT_TIMESTAMP)
        ON CONFLICT(cluster_id, email) DO UPDATE SET
            provider = excluded.provider,
            status = excluded.status,
            capabilities = excluded.capabilities,
            daily_limit = excluded.daily_limit,
            timezone = excluded.timezone,
            approved_at = {approved_expr},
            removed_at = {removed_expr},
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            (cluster_id or "").strip(),
            clean_email,
            provider,
            next_status,
            capabilities,
            int(daily_limit or 5),
            timezone,
        ),
    )
    conn.commit()
    conn.close()
    return True


def list_warm_cluster_members(cluster_id=""):
    conn = get_connection()
    cursor = conn.cursor()
    params = []
    where = ""
    if cluster_id:
        where = "WHERE cluster_id = ?"
        params.append(cluster_id)
    cursor.execute(
        f"""
        SELECT cluster_id, email, provider, status, capabilities, daily_limit,
               timezone, approved_at, removed_at, last_seen_at, created_at, updated_at
        FROM warm_cluster_members
        {where}
        ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END, email
        """,
        params,
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def update_warm_cluster_member_status(cluster_id, email, status):
    next_status = status if status in {"pending", "active", "paused", "blacklisted"} else "pending"
    conn = get_connection()
    cursor = conn.cursor()
    updates = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params = [next_status]
    if next_status == "active":
        updates.append("approved_at = CURRENT_TIMESTAMP")
    if next_status == "blacklisted":
        updates.append("removed_at = CURRENT_TIMESTAMP")
    params.extend([(cluster_id or "").strip(), (email or "").strip().lower()])
    cursor.execute(
        f"UPDATE warm_cluster_members SET {', '.join(updates)} WHERE cluster_id = ? AND email = ?",
        params,
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed


def delete_warm_cluster_member(cluster_id, email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM warm_cluster_members WHERE cluster_id = ? AND email = ?",
        ((cluster_id or "").strip(), (email or "").strip().lower()),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed
