import base64
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_BASE_URL,
    ANTHROPIC_MODEL,
    DB_PATH,
    DEFAULT_LLM_PROVIDER,
    DEFAULT_SYSTEM_PROMPT,
    LEGACY_BRIEF_SYSTEM_PROMPT,
    LEGACY_DEFAULT_SYSTEM_PROMPT,
    DEFAULT_DAILY_LIMIT,
    MAIL_FROM_NAME,
    MAILFORGE_IMAP_HOST,
    MAILFORGE_IMAP_PORT,
    MAILFORGE_SMTP_HOST,
    MAILFORGE_SMTP_PORT,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
    WARM_REPLY_HARD_TIMEOUT_HOURS,
    WARM_REPLY_MIN_DELAY_HOURS,
    WARM_MAILBOX_OFFLINE_WARN_SEC,
    WARM_MAILBOX_STALE_SEC,
    WARM_SCAN_HARD_TIMEOUT_HOURS,
    WARM_SCAN_SOFT_TIMEOUT_HOURS,
)

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover - keeps old installs bootable until requirements are installed
    Fernet = None


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


LLM_PURPOSE_PROVIDERS = {
    "cold": {"openai", "anthropic"},
    "warm": {"warm_openai", "warm_anthropic"},
}


def _llm_purpose(provider):
    return "warm" if (provider or "").startswith("warm_") else "cold"


def _llm_base_provider(provider):
    provider = (provider or "").strip().lower()
    return provider[5:] if provider.startswith("warm_") else provider


def _llm_display_name(provider):
    base = _llm_base_provider(provider)
    label = "OpenAI" if base == "openai" else "Anthropic Claude"
    return f"Warm {label}" if _llm_purpose(provider) == "warm" else label


WARM_LLM_SYSTEM_PROMPT = (
    "You write plain, low-stakes mailbox warm conversation content. "
    "Your job is to make short, normal messages that sound like real people writing casual work notes or light personal check-ins. "
    "Never write sales outreach, promotions, lead generation, deliverability language, spam-filter language, or anything that reveals automation. "
    "Use simple human variety: brief business coordination, document notes, schedule checks, sports, fitness, weekend plans, holidays, congratulations, or small everyday updates. "
    "Keep subjects short, bodies concise, and replies context-aware. Output exactly what the user asks for."
)


CRM_STATUSES = {
    "pending",
    "replied_pending_review",
    "interested",
    "follow_up_later",
    "not_interested",
    "bounced",
    "abandoned",
}

CRM_AUTO_EXCLUDED_STATUSES = {
    "replied_pending_review",
    "interested",
    "follow_up_later",
    "not_interested",
    "bounced",
    "abandoned",
}

CRM_DEFAULT_REMARKETING_MAX = 3
CRM_HARD_REMARKETING_MAX = 4
CRM_INTERESTED_FOLDER = "INBOX/Interested Leads"
CRM_EXTERNAL_TOUCH_STATUSES = {"none", "pending", "in_progress", "done", "paused"}


def _clean_email(value):
    return (value or "").strip().lower()


def _crm_status(value, default="pending"):
    value = (value or "").strip().lower()
    return value if value in CRM_STATUSES else default


def _crm_external_status(value, default="none"):
    value = (value or "").strip().lower()
    return value if value in CRM_EXTERNAL_TOUCH_STATUSES else default


def _safe_json_loads(value, default=None):
    if default is None:
        default = {}
    try:
        loaded = json.loads(value or "")
        return loaded if isinstance(loaded, type(default)) else default
    except Exception:
        return default


def _clean_tag_name(value):
    value = " ".join(str(value or "").strip().split())
    return value[:80]


def _normalize_tag_names(tags):
    if isinstance(tags, str):
        tags = tags.replace(";", ",").split(",")
    seen = set()
    clean = []
    for tag in tags or []:
        name = _clean_tag_name(tag)
        key = name.lower()
        if name and key not in seen:
            clean.append(name)
            seen.add(key)
    return clean


def _insert_default_llm_settings(cursor):
    defaults = [
        (
            "openai",
            "OpenAI",
            OPENAI_API_KEY,
            OPENAI_BASE_URL,
            OPENAI_MODEL,
            "active" if DEFAULT_LLM_PROVIDER == "openai" else "inactive",
        ),
        (
            "anthropic",
            "Anthropic Claude",
            ANTHROPIC_API_KEY,
            ANTHROPIC_BASE_URL,
            ANTHROPIC_MODEL,
            "active" if DEFAULT_LLM_PROVIDER == "anthropic" else "inactive",
        ),
        (
            "warm_openai",
            "Warm OpenAI",
            OPENAI_API_KEY,
            OPENAI_BASE_URL,
            "gpt-4o-mini",
            WARM_LLM_SYSTEM_PROMPT,
            "active",
        ),
        (
            "warm_anthropic",
            "Warm Anthropic Claude",
            ANTHROPIC_API_KEY,
            ANTHROPIC_BASE_URL,
            "claude-3-haiku-20240307",
            WARM_LLM_SYSTEM_PROMPT,
            "inactive",
        ),
    ]
    for item in defaults:
        if len(item) == 6:
            provider, display_name, api_key, base_url, model, status = item
            system_prompt = DEFAULT_SYSTEM_PROMPT
        else:
            provider, display_name, api_key, base_url, model, system_prompt, status = item
        cursor.execute("SELECT 1 FROM llm_settings WHERE provider = ?", (provider,))
        if cursor.fetchone():
            continue
        cursor.execute(
            """
            INSERT INTO llm_settings (
                provider, display_name, api_key_cipher, base_url, model,
                system_prompt, status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                provider,
                display_name,
                _encrypt_secret(api_key),
                base_url,
                model,
                system_prompt,
                status,
            ),
        )


def _refresh_default_llm_prompt(cursor):
    cursor.execute(
        """
        UPDATE llm_settings
        SET system_prompt = ?, updated_at = CURRENT_TIMESTAMP
        WHERE provider IN ('openai', 'anthropic')
          AND (
              system_prompt IS NULL
              OR TRIM(system_prompt) = ''
              OR system_prompt = ?
              OR system_prompt = ?
              OR (system_prompt LIKE ? AND system_prompt NOT LIKE ?)
          )
        """,
        (
            DEFAULT_SYSTEM_PROMPT,
            LEGACY_DEFAULT_SYSTEM_PROMPT,
            LEGACY_BRIEF_SYSTEM_PROMPT,
            "You are ePetrel's deliverability-aware B2B outbound email copywriter.%",
            "%De-market promotional copy%",
        ),
    )
    cursor.execute(
        """
        UPDATE llm_settings
        SET system_prompt = ?, updated_at = CURRENT_TIMESTAMP
        WHERE provider IN ('warm_openai', 'warm_anthropic')
          AND (
              system_prompt IS NULL
              OR TRIM(system_prompt) = ''
              OR system_prompt = ?
              OR system_prompt = ?
              OR system_prompt = ?
              OR system_prompt LIKE ?
          )
        """,
        (
            WARM_LLM_SYSTEM_PROMPT,
            DEFAULT_SYSTEM_PROMPT,
            LEGACY_DEFAULT_SYSTEM_PROMPT,
            LEGACY_BRIEF_SYSTEM_PROMPT,
            "You are ePetrel's deliverability-aware B2B outbound email copywriter.%",
        ),
    )


def _insert_default_remarketing_templates(cursor):
    defaults = [
        (
            1,
            "Remarketing 1",
            "Quick follow-up for {Company}",
            "Hi {Name},\n\nI wanted to follow up on my note about {Company}. If this is relevant, I can send a short example.\n\nWould it be useful to take a look?",
            "If this is not relevant, just reply no and I will not follow up again.",
            "BR\n{Sender_Name}",
            4,
        ),
        (
            2,
            "Remarketing 2",
            "Worth revisiting?",
            "Hi {Name},\n\nChecking once more in case the earlier note got buried. The idea was to share a concise way this could help {Company}.\n\nShould I send the details?",
            "If this is not a fit, reply no and I will close the loop.",
            "BR\n{Sender_Name}",
            7,
        ),
        (
            3,
            "Remarketing 3",
            "Closing the loop",
            "Hi {Name},\n\nI do not want to keep filling your inbox. If the idea for {Company} is useful, I can send one short example. Otherwise I will close this out.\n\nAny preference?",
            "Reply no if you would rather not hear from me again.",
            "BR\n{Sender_Name}",
            10,
        ),
        (
            4,
            "Remarketing 4",
            "Final note",
            "Hi {Name},\n\nLast quick note from me. If this is still useful for {Company}, I can send the short version. If not, no worries.",
            "Reply no and I will remove you from follow-up.",
            "BR\n{Sender_Name}",
            14,
        ),
    ]
    for row in defaults:
        cursor.execute("SELECT 1 FROM remarketing_templates WHERE step_number = ?", (row[0],))
        if cursor.fetchone():
            continue
        cursor.execute(
            """
            INSERT INTO remarketing_templates (
                step_number, name, subject, body, unsubscribe_copy, signature,
                cooldown_days, status, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """,
            row,
        )


def init_db():
    """初始化 SQLite 数据库表结构"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. 马甲发件箱表（包含状态和熔断计数）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS senders (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            daily_limit INTEGER DEFAULT 40,
            fail_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active' -- active / paused
        )
    ''')
    _add_column_if_missing(cursor, "senders", "daily_sent_count", "INTEGER DEFAULT 0")
    _add_column_if_missing(cursor, "senders", "last_reset_date", "TEXT")
    _add_column_if_missing(cursor, "senders", "last_sent_at", "DATETIME")
    _add_column_if_missing(cursor, "senders", "smtp_host", "TEXT")
    _add_column_if_missing(cursor, "senders", "smtp_port", "INTEGER")
    _add_column_if_missing(cursor, "senders", "imap_host", "TEXT")
    _add_column_if_missing(cursor, "senders", "imap_port", "INTEGER")
    _add_column_if_missing(cursor, "senders", "from_name", "TEXT")
    _add_column_if_missing(cursor, "senders", "reply_to_email", "TEXT")
    _add_column_if_missing(cursor, "senders", "smtp_check_status", "TEXT DEFAULT 'unchecked'")
    _add_column_if_missing(cursor, "senders", "imap_check_status", "TEXT DEFAULT 'unchecked'")
    _add_column_if_missing(cursor, "senders", "mailbox_check_status", "TEXT DEFAULT 'unchecked'")
    _add_column_if_missing(cursor, "senders", "last_checked_at", "DATETIME")
    _add_column_if_missing(cursor, "senders", "check_error", "TEXT")
    _add_column_if_missing(cursor, "senders", "auth_method", "TEXT DEFAULT 'smtp'")
    _add_column_if_missing(cursor, "senders", "gmail_client_id", "TEXT")
    _add_column_if_missing(cursor, "senders", "gmail_client_secret_cipher", "TEXT")
    _add_column_if_missing(cursor, "senders", "gmail_refresh_token_cipher", "TEXT")
    _add_column_if_missing(cursor, "senders", "gmail_token_status", "TEXT DEFAULT 'not_connected'")
    _add_column_if_missing(cursor, "senders", "gmail_granted_scopes", "TEXT")
    _add_column_if_missing(cursor, "senders", "gmail_account_type", "TEXT DEFAULT ''")
    cursor.execute(
        """
        UPDATE senders
        SET gmail_account_type = CASE
            WHEN auth_method = 'gmail_api'
                 AND (LOWER(email) LIKE '%@gmail.com' OR LOWER(email) LIKE '%@googlemail.com')
                THEN 'consumer_gmail'
            WHEN auth_method = 'gmail_api'
                THEN 'workspace_gmail'
            ELSE 'smtp_generic'
        END
        WHERE gmail_account_type IS NULL OR TRIM(gmail_account_type) = ''
        """
    )
    
    # 2. 发信全留底审计表（方便回头审查）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS outbound_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            sender TEXT,
            receiver TEXT,
            subject TEXT,
            body_html TEXT,
            variant_version TEXT,
            status TEXT -- success / failed / skipped
        )
    ''')
    _add_column_if_missing(cursor, "outbound_logs", "plain_text", "TEXT")
    _add_column_if_missing(cursor, "outbound_logs", "message_id", "TEXT")
    _add_column_if_missing(cursor, "outbound_logs", "target_domain", "TEXT")
    _add_column_if_missing(cursor, "outbound_logs", "error", "TEXT")
    _add_column_if_missing(cursor, "outbound_logs", "crm_remarketing_step", "INTEGER DEFAULT 0")
    _add_column_if_missing(cursor, "outbound_logs", "crm_template_name", "TEXT")
    
    # 3. 统一共享收件箱表（回信聚合与 AI 意图打标）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inbound_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at DATETIME,
            sender TEXT, -- 客户邮箱
            receiver TEXT, -- 我们的马甲号
            subject TEXT,
            content TEXT,
            sentiment TEXT DEFAULT 'Pending' -- 意向分类：高意向 / 拒绝 / 稍后跟进
        )
    ''')
    _add_column_if_missing(cursor, "inbound_emails", "message_id", "TEXT")
    _add_column_if_missing(cursor, "inbound_emails", "imap_uid", "TEXT")
    _add_column_if_missing(cursor, "inbound_emails", "imap_folder", "TEXT DEFAULT 'INBOX'")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS suppression_list (
            email TEXT PRIMARY KEY,
            reason TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS domain_counters (
            domain TEXT,
            send_date TEXT,
            sent_count INTEGER DEFAULT 0,
            PRIMARY KEY (domain, send_date)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS email_test_domain_counters (
            domain TEXT,
            test_date TEXT,
            test_count INTEGER DEFAULT 0,
            PRIMARY KEY (domain, test_date)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS delivery_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            sender TEXT,
            receiver TEXT,
            event_type TEXT,
            source TEXT,
            subject TEXT,
            message_id TEXT,
            source_message_id TEXT,
            target_domain TEXT,
            severity TEXT,
            details TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_delivery_events_time ON delivery_events(event_time)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_delivery_events_type ON delivery_events(event_type)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_delivery_events_receiver ON delivery_events(receiver)"
    )

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS seed_accounts (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            provider TEXT,
            imap_host TEXT NOT NULL,
            imap_port INTEGER DEFAULT 993,
            inbox_folder TEXT DEFAULT 'INBOX',
            spam_folder TEXT DEFAULT 'Spam',
            status TEXT DEFAULT 'active',
            last_checked_at DATETIME
        )
    ''')

    cursor.execute('''
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
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS email_templates (
            slot_number INTEGER PRIMARY KEY,
            name TEXT,
            subject TEXT,
            body TEXT,
            unsubscribe_copy TEXT,
            signature TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS crm_contacts (
            email TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            company TEXT DEFAULT '',
            position TEXT DEFAULT '',
            company_bio TEXT DEFAULT '',
            website TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            country TEXT DEFAULT '',
            source TEXT DEFAULT '',
            campaign TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            notes TEXT DEFAULT '',
            next_followup_at DATETIME,
            remarketing_attempts INTEGER DEFAULT 0,
            max_remarketing_attempts INTEGER DEFAULT 3,
            last_sent_at DATETIME,
            last_reply_at DATETIME,
            owner_sender TEXT DEFAULT '',
            whatsapp TEXT DEFAULT '',
            instagram TEXT DEFAULT '',
            linkedin TEXT DEFAULT '',
            external_touch_status TEXT DEFAULT 'none',
            external_touch_channel TEXT DEFAULT '',
            enrichment_status TEXT DEFAULT 'missing',
            custom_fields_json TEXT DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    for column, definition in [
        ("name", "TEXT DEFAULT ''"),
        ("company", "TEXT DEFAULT ''"),
        ("position", "TEXT DEFAULT ''"),
        ("company_bio", "TEXT DEFAULT ''"),
        ("website", "TEXT DEFAULT ''"),
        ("phone", "TEXT DEFAULT ''"),
        ("country", "TEXT DEFAULT ''"),
        ("source", "TEXT DEFAULT ''"),
        ("campaign", "TEXT DEFAULT ''"),
        ("status", "TEXT DEFAULT 'pending'"),
        ("notes", "TEXT DEFAULT ''"),
        ("next_followup_at", "DATETIME"),
        ("remarketing_attempts", "INTEGER DEFAULT 0"),
        ("max_remarketing_attempts", "INTEGER DEFAULT 3"),
        ("last_sent_at", "DATETIME"),
        ("last_reply_at", "DATETIME"),
        ("owner_sender", "TEXT DEFAULT ''"),
        ("whatsapp", "TEXT DEFAULT ''"),
        ("instagram", "TEXT DEFAULT ''"),
        ("linkedin", "TEXT DEFAULT ''"),
        ("external_touch_status", "TEXT DEFAULT 'none'"),
        ("external_touch_channel", "TEXT DEFAULT ''"),
        ("enrichment_status", "TEXT DEFAULT 'missing'"),
        ("custom_fields_json", "TEXT DEFAULT '{}'"),
        ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ("updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ]:
        _add_column_if_missing(cursor, "crm_contacts", column, definition)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_contacts_status ON crm_contacts(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_contacts_next_followup ON crm_contacts(next_followup_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_contacts_campaign ON crm_contacts(campaign)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_contacts_company ON crm_contacts(company)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_contacts_external ON crm_contacts(external_touch_status)")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS crm_activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_email TEXT NOT NULL,
            activity_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            activity_type TEXT,
            status TEXT,
            summary TEXT,
            content_snapshot TEXT,
            actor TEXT DEFAULT 'system',
            outbound_log_id INTEGER,
            inbound_email_id INTEGER,
            delivery_event_id INTEGER,
            remarketing_step INTEGER DEFAULT 0,
            template_name TEXT DEFAULT '',
            variant_version TEXT DEFAULT '',
            channel TEXT DEFAULT '',
            metadata_json TEXT DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_activities_contact_time ON crm_activities(contact_email, activity_time)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_activities_type_time ON crm_activities(activity_type, activity_time)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_activities_outbound ON crm_activities(outbound_log_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_activities_inbound ON crm_activities(inbound_email_id)")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS remarketing_templates (
            step_number INTEGER PRIMARY KEY,
            name TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            body TEXT DEFAULT '',
            unsubscribe_copy TEXT DEFAULT '',
            signature TEXT DEFAULT '',
            cooldown_days INTEGER DEFAULT 7,
            status TEXT DEFAULT 'active',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS crm_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            color TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS crm_contact_tags (
            contact_email TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (contact_email, tag_id)
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_contact_tags_tag ON crm_contact_tags(tag_id)")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS crm_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_email TEXT NOT NULL,
            task_type TEXT DEFAULT 'custom',
            title TEXT DEFAULT '',
            due_at DATETIME,
            status TEXT DEFAULT 'open',
            notes TEXT DEFAULT '',
            channel TEXT DEFAULT '',
            completed_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_tasks_contact ON crm_tasks(contact_email)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_tasks_due ON crm_tasks(status, due_at)")
    _insert_default_remarketing_templates(cursor)

    cursor.execute('''
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
    ''')

    cursor.execute('''
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
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warm_cluster_members_status ON warm_cluster_members(cluster_id, status)"
    )

    cursor.execute('''
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
    ''')
    _add_column_if_missing(cursor, "warm_mailboxes", "cluster_id", "TEXT DEFAULT ''")
    _add_column_if_missing(cursor, "warm_mailboxes", "scan_soft_timeout_hours", f"INTEGER DEFAULT {WARM_SCAN_SOFT_TIMEOUT_HOURS}")
    _add_column_if_missing(cursor, "warm_mailboxes", "scan_hard_timeout_hours", f"INTEGER DEFAULT {WARM_SCAN_HARD_TIMEOUT_HOURS}")
    _add_column_if_missing(cursor, "warm_mailboxes", "reply_min_delay_hours", f"INTEGER DEFAULT {WARM_REPLY_MIN_DELAY_HOURS}")
    _add_column_if_missing(cursor, "warm_mailboxes", "reply_hard_timeout_hours", f"INTEGER DEFAULT {WARM_REPLY_HARD_TIMEOUT_HOURS}")
    _add_column_if_missing(cursor, "warm_mailboxes", "avoid_sleep_hours", "INTEGER DEFAULT 1")
    _add_column_if_missing(cursor, "warm_mailboxes", "avoid_weekends", "INTEGER DEFAULT 1")

    cursor.execute('''
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
    ''')
    _add_column_if_missing(cursor, "warm_events", "cluster_id", "TEXT DEFAULT ''")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warm_events_mailbox_time ON warm_events(mailbox_email, event_time)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warm_events_type_time ON warm_events(event_type, event_time)"
    )
    cursor.execute('''
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
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warm_local_tasks_status ON warm_local_tasks(status, updated_at)"
    )
    cursor.execute('''
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
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warm_worker_state_updated ON warm_worker_state(updated_at)"
    )
    cursor.execute('''
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
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warm_local_threads_pair ON warm_local_threads(cluster_id, sender_email, peer_email)"
    )
    cursor.execute('''
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
    ''')
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warm_content_hashes ON warm_content_fingerprints(subject_hash, body_hash, created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_warm_content_pair_time ON warm_content_fingerprints(cluster_id, sender_email, receiver_email, created_at)"
    )
    _insert_default_llm_settings(cursor)
    _refresh_default_llm_prompt(cursor)
    
    conn.commit()
    conn.close()


def _today():
    return date.today().isoformat()


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
    _sync_sender_daily_counts_from_audit(cursor)


def _sync_sender_daily_counts_from_audit(cursor, email=None):
    # The audit table is the source of truth, but only today's successful sends
    # count toward a sender's daily limit. Historical rows from previous days
    # must remain visible without consuming today's quota.
    today = _today()
    params = [today]
    sender_filter = ""
    if email:
        sender_filter = "AND LOWER(sender) = ?"
        params.append((email or "").strip().lower())

    cursor.execute(
        f"""
        SELECT LOWER(sender) AS sender, COUNT(*) AS sent_count
        FROM outbound_logs
        WHERE status = 'success'
          AND date(timestamp, 'localtime') = ?
          AND COALESCE(sender, '') != ''
          {sender_filter}
        GROUP BY LOWER(sender)
        """,
        params,
    )
    counts = {row[0]: int(row[1] or 0) for row in cursor.fetchall()}
    if email:
        cursor.execute("SELECT LOWER(email) AS email FROM senders WHERE LOWER(email) = ?", ((email or "").strip().lower(),))
    else:
        cursor.execute("SELECT LOWER(email) AS email FROM senders")
    sender_emails = [row[0] for row in cursor.fetchall()]
    for email in sender_emails:
        cursor.execute(
            """
            UPDATE senders
            SET daily_sent_count = ?
            WHERE LOWER(email) = ?
            """,
            (counts.get(email, 0), email),
        )


def refresh_sender_daily_counts(email=None):
    conn = get_connection()
    cursor = conn.cursor()
    reset_daily_counters_if_needed(cursor)
    if email:
        _sync_sender_daily_counts_from_audit(cursor, email)
    conn.commit()
    conn.close()


def upsert_sender(
    email,
    password,
    daily_limit=DEFAULT_DAILY_LIMIT,
    status="active",
    smtp_host=None,
    smtp_port=None,
    imap_host=None,
    imap_port=None,
    from_name=None,
    reply_to_email=None,
    smtp_check_status="unchecked",
    imap_check_status="unchecked",
    mailbox_check_status="unchecked",
    check_error="",
    auth_method="smtp",
    gmail_client_id=None,
    gmail_client_secret=None,
    gmail_refresh_token=None,
    gmail_token_status=None,
    gmail_granted_scopes=None,
    gmail_account_type=None,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM senders WHERE email = ?", (email.strip().lower(),))
    existing = dict(cursor.fetchone() or {})

    def secret_cipher(value, column):
        if value is None:
            return existing.get(column, "")
        return _encrypt_secret(value)

    cursor.execute(
        """
        INSERT INTO senders (
            email, password, daily_limit, status, smtp_host, smtp_port,
            imap_host, imap_port, from_name, reply_to_email, last_reset_date,
            smtp_check_status, imap_check_status, mailbox_check_status,
            last_checked_at, check_error, auth_method, gmail_client_id,
            gmail_client_secret_cipher, gmail_refresh_token_cipher,
            gmail_token_status, gmail_granted_scopes, gmail_account_type
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            password = excluded.password,
            daily_limit = excluded.daily_limit,
            status = excluded.status,
            smtp_host = excluded.smtp_host,
            smtp_port = excluded.smtp_port,
            imap_host = excluded.imap_host,
            imap_port = excluded.imap_port,
            from_name = excluded.from_name,
            reply_to_email = excluded.reply_to_email,
            smtp_check_status = excluded.smtp_check_status,
            imap_check_status = excluded.imap_check_status,
            mailbox_check_status = excluded.mailbox_check_status,
            last_checked_at = excluded.last_checked_at,
            check_error = excluded.check_error,
            auth_method = excluded.auth_method,
            gmail_client_id = excluded.gmail_client_id,
            gmail_client_secret_cipher = excluded.gmail_client_secret_cipher,
            gmail_refresh_token_cipher = excluded.gmail_refresh_token_cipher,
            gmail_token_status = excluded.gmail_token_status,
            gmail_granted_scopes = excluded.gmail_granted_scopes,
            gmail_account_type = excluded.gmail_account_type
        """,
        (
            email.strip().lower(),
            password or "",
            daily_limit,
            status,
            smtp_host or MAILFORGE_SMTP_HOST,
            smtp_port or MAILFORGE_SMTP_PORT,
            imap_host or MAILFORGE_IMAP_HOST,
            imap_port or MAILFORGE_IMAP_PORT,
            from_name or MAIL_FROM_NAME,
            reply_to_email or email.strip().lower(),
            _today(),
            smtp_check_status,
            imap_check_status,
            mailbox_check_status,
            check_error,
            auth_method or existing.get("auth_method") or "smtp",
            gmail_client_id if gmail_client_id is not None else existing.get("gmail_client_id", ""),
            secret_cipher(gmail_client_secret, "gmail_client_secret_cipher"),
            secret_cipher(gmail_refresh_token, "gmail_refresh_token_cipher"),
            gmail_token_status if gmail_token_status is not None else existing.get("gmail_token_status", "not_connected"),
            gmail_granted_scopes if gmail_granted_scopes is not None else existing.get("gmail_granted_scopes", ""),
            gmail_account_type if gmail_account_type is not None else existing.get("gmail_account_type", "smtp_generic"),
        ),
    )
    conn.commit()
    conn.close()


def list_senders(include_credentials=False):
    conn = get_connection()
    cursor = conn.cursor()
    reset_daily_counters_if_needed(cursor)
    password_column = ", password" if include_credentials else ""
    cursor.execute(
        f"""
        SELECT email, daily_limit, daily_sent_count, fail_count, status,
               smtp_host, smtp_port, imap_host, imap_port, from_name, reply_to_email,
               smtp_check_status, imap_check_status, mailbox_check_status,
               last_checked_at, check_error, auth_method, gmail_token_status,
               gmail_granted_scopes, gmail_account_type
               {password_column}
        FROM senders
        ORDER BY status, email
        """
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.commit()
    conn.close()
    return rows


def get_sender(email):
    conn = get_connection()
    cursor = conn.cursor()
    reset_daily_counters_if_needed(cursor)
    cursor.execute("SELECT * FROM senders WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.commit()
    conn.close()
    if not row:
        return None
    data = dict(row)
    data["gmail_client_secret"] = _decrypt_secret(data.pop("gmail_client_secret_cipher", ""))
    data["gmail_refresh_token"] = _decrypt_secret(data.pop("gmail_refresh_token_cipher", ""))
    return data


def delete_sender(email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM senders WHERE email = ?", (email.strip().lower(),))
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


def add_suppression(email, reason="manual"):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO suppression_list (email, reason)
        VALUES (?, ?)
        ON CONFLICT(email) DO UPDATE SET reason = excluded.reason
        """,
        (email.strip().lower(), reason),
    )
    conn.commit()
    conn.close()


def upsert_seed_account(
    email,
    password,
    provider="",
    imap_host="",
    imap_port=993,
    inbox_folder="INBOX",
    spam_folder="Spam",
    status="active",
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO seed_accounts (
            email, password, provider, imap_host, imap_port,
            inbox_folder, spam_folder, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            password = excluded.password,
            provider = excluded.provider,
            imap_host = excluded.imap_host,
            imap_port = excluded.imap_port,
            inbox_folder = excluded.inbox_folder,
            spam_folder = excluded.spam_folder,
            status = excluded.status
        """,
        (
            email.strip().lower(),
            password,
            provider,
            imap_host,
            int(imap_port or 993),
            inbox_folder or "INBOX",
            spam_folder or "Spam",
            status,
        ),
    )
    conn.commit()
    conn.close()


def list_seed_accounts(include_credentials=False, active_only=False):
    conn = get_connection()
    cursor = conn.cursor()
    password_column = ", password" if include_credentials else ""
    where_clause = "WHERE status = 'active'" if active_only else ""
    cursor.execute(
        f"""
        SELECT email, provider, imap_host, imap_port, inbox_folder,
               spam_folder, status, last_checked_at {password_column}
        FROM seed_accounts
        {where_clause}
        ORDER BY status, provider, email
        """
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def clear_seed_accounts():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM seed_accounts")
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def mark_seed_checked(email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE seed_accounts SET last_checked_at = CURRENT_TIMESTAMP WHERE email = ?",
        (email.strip().lower(),),
    )
    conn.commit()
    conn.close()


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


def list_email_templates(limit=5):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT slot_number, name, subject, body, unsubscribe_copy, signature, updated_at
        FROM email_templates
        WHERE slot_number BETWEEN 1 AND ?
        ORDER BY slot_number
        """,
        (int(limit or 5),),
    )
    saved = {int(row["slot_number"]): dict(row) for row in cursor.fetchall()}
    conn.close()
    return [
        saved.get(slot)
        or {
            "slot_number": slot,
            "name": "",
            "subject": "",
            "body": "",
            "unsubscribe_copy": "",
            "signature": "",
            "updated_at": "",
        }
        for slot in range(1, int(limit or 5) + 1)
    ]


def get_email_template(slot_number):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT slot_number, name, subject, body, unsubscribe_copy, signature, updated_at
        FROM email_templates
        WHERE slot_number = ?
        """,
        (int(slot_number or 0),),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_email_template(slot_number, name, subject, body, unsubscribe_copy, signature):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO email_templates (
            slot_number, name, subject, body, unsubscribe_copy, signature, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(slot_number) DO UPDATE SET
            name = excluded.name,
            subject = excluded.subject,
            body = excluded.body,
            unsubscribe_copy = excluded.unsubscribe_copy,
            signature = excluded.signature,
            updated_at = excluded.updated_at
        """,
        (
            int(slot_number or 0),
            name,
            subject,
            body,
            unsubscribe_copy,
            signature,
        ),
    )
    conn.commit()
    conn.close()


def delete_email_template(slot_number):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM email_templates WHERE slot_number = ?", (int(slot_number or 0),))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def _crm_max_attempts(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = CRM_DEFAULT_REMARKETING_MAX
    return max(0, min(CRM_HARD_REMARKETING_MAX, value))


def _crm_contact_dict(row):
    data = dict(row or {})
    data["custom_fields"] = _safe_json_loads(data.get("custom_fields_json"), {})
    data["tags"] = []
    return data


def add_crm_tags(contact_email, tags):
    contact_email = _clean_email(contact_email)
    tags = _normalize_tag_names(tags)
    if not contact_email or not tags:
        return []
    conn = get_connection()
    cursor = conn.cursor()
    saved = []
    for name in tags:
        cursor.execute(
            """
            INSERT INTO crm_tags (name)
            VALUES (?)
            ON CONFLICT(name) DO NOTHING
            """,
            (name,),
        )
        cursor.execute("SELECT id, name FROM crm_tags WHERE LOWER(name) = LOWER(?)", (name,))
        row = cursor.fetchone()
        if not row:
            continue
        cursor.execute(
            """
            INSERT INTO crm_contact_tags (contact_email, tag_id)
            VALUES (?, ?)
            ON CONFLICT(contact_email, tag_id) DO NOTHING
            """,
            (contact_email, row["id"]),
        )
        saved.append(row["name"])
    conn.commit()
    conn.close()
    return saved


def remove_crm_tags(contact_email, tags):
    contact_email = _clean_email(contact_email)
    tags = _normalize_tag_names(tags)
    if not contact_email or not tags:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in tags)
    cursor.execute(
        f"""
        DELETE FROM crm_contact_tags
        WHERE contact_email = ?
          AND tag_id IN (
              SELECT id FROM crm_tags WHERE LOWER(name) IN ({placeholders})
          )
        """,
        [contact_email, *(tag.lower() for tag in tags)],
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed


def list_crm_tags():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT t.name, COUNT(ct.contact_email) AS contact_count
        FROM crm_tags t
        LEFT JOIN crm_contact_tags ct ON ct.tag_id = t.id
        GROUP BY t.id, t.name
        ORDER BY LOWER(t.name)
        """
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_crm_contact_tags(contact_email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT t.name
        FROM crm_contact_tags ct
        JOIN crm_tags t ON t.id = ct.tag_id
        WHERE ct.contact_email = ?
        ORDER BY LOWER(t.name)
        """,
        (_clean_email(contact_email),),
    )
    tags = [row["name"] for row in cursor.fetchall()]
    conn.close()
    return tags


def upsert_crm_contact(
    email,
    name=None,
    company=None,
    position=None,
    company_bio=None,
    website=None,
    phone=None,
    country=None,
    source=None,
    campaign=None,
    status=None,
    notes=None,
    whatsapp=None,
    instagram=None,
    linkedin=None,
    custom_fields=None,
    tags=None,
    max_remarketing_attempts=None,
    next_followup_at=None,
):
    email = _clean_email(email)
    if not email:
        return None

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM crm_contacts WHERE email = ?", (email,))
    existing = dict(cursor.fetchone() or {})
    existing_custom = _safe_json_loads(existing.get("custom_fields_json"), {})
    merged_custom = dict(existing_custom)
    for key, value in (custom_fields or {}).items():
        key = str(key or "").strip()
        if not key:
            continue
        if value is None:
            continue
        text = str(value).strip()
        if text:
            merged_custom[key] = text

    def pick(field_name, value, default=""):
        if value is None:
            return existing.get(field_name, default)
        text = str(value).strip()
        if text == "" and existing:
            return existing.get(field_name, default)
        return text

    next_status = _crm_status(status, existing.get("status") or "pending") if status else (existing.get("status") or "pending")
    next_external_status = existing.get("external_touch_status") or "none"
    max_attempts = (
        _crm_max_attempts(max_remarketing_attempts)
        if max_remarketing_attempts is not None
        else _crm_max_attempts(existing.get("max_remarketing_attempts", CRM_DEFAULT_REMARKETING_MAX))
    )

    cursor.execute(
        """
        INSERT INTO crm_contacts (
            email, name, company, position, company_bio, website, phone, country,
            source, campaign, status, notes, next_followup_at, remarketing_attempts,
            max_remarketing_attempts, last_sent_at, last_reply_at, owner_sender,
            whatsapp, instagram, linkedin, external_touch_status, external_touch_channel,
            enrichment_status, custom_fields_json, created_at, updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP
        )
        ON CONFLICT(email) DO UPDATE SET
            name = excluded.name,
            company = excluded.company,
            position = excluded.position,
            company_bio = excluded.company_bio,
            website = excluded.website,
            phone = excluded.phone,
            country = excluded.country,
            source = excluded.source,
            campaign = excluded.campaign,
            status = excluded.status,
            notes = excluded.notes,
            next_followup_at = excluded.next_followup_at,
            max_remarketing_attempts = excluded.max_remarketing_attempts,
            whatsapp = excluded.whatsapp,
            instagram = excluded.instagram,
            linkedin = excluded.linkedin,
            external_touch_status = excluded.external_touch_status,
            enrichment_status = excluded.enrichment_status,
            custom_fields_json = excluded.custom_fields_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            email,
            pick("name", name),
            pick("company", company),
            pick("position", position),
            pick("company_bio", company_bio),
            pick("website", website),
            pick("phone", phone),
            pick("country", country),
            pick("source", source),
            pick("campaign", campaign),
            next_status,
            pick("notes", notes),
            next_followup_at if next_followup_at is not None else existing.get("next_followup_at"),
            int(existing.get("remarketing_attempts") or 0),
            max_attempts,
            existing.get("last_sent_at"),
            existing.get("last_reply_at"),
            existing.get("owner_sender", ""),
            pick("whatsapp", whatsapp),
            pick("instagram", instagram),
            pick("linkedin", linkedin),
            _crm_external_status(next_external_status),
            existing.get("external_touch_channel", ""),
            existing.get("enrichment_status") or "missing",
            json_dumps(merged_custom),
            existing.get("created_at"),
        ),
    )
    conn.commit()
    conn.close()
    if tags:
        add_crm_tags(email, tags)
    return get_crm_contact(email)


def get_crm_contact(email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM crm_contacts WHERE email = ?", (_clean_email(email),))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    contact = _crm_contact_dict(row)
    contact["tags"] = get_crm_contact_tags(contact["email"])
    return contact


def crm_contact_auto_excluded(email):
    email = _clean_email(email)
    if not email:
        return True
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.status, s.email AS suppressed
        FROM crm_contacts c
        LEFT JOIN suppression_list s ON s.email = c.email
        WHERE c.email = ?
        """,
        (email,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return False
    return bool(row["suppressed"]) or (row["status"] in CRM_AUTO_EXCLUDED_STATUSES)


def log_crm_activity(
    contact_email,
    activity_type,
    summary="",
    content_snapshot="",
    status="",
    actor="system",
    outbound_log_id=None,
    inbound_email_id=None,
    delivery_event_id=None,
    remarketing_step=0,
    template_name="",
    variant_version="",
    channel="",
    metadata=None,
    activity_time=None,
):
    contact_email = _clean_email(contact_email)
    if not contact_email:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO crm_activities (
            contact_email, activity_time, activity_type, status, summary,
            content_snapshot, actor, outbound_log_id, inbound_email_id,
            delivery_event_id, remarketing_step, template_name, variant_version,
            channel, metadata_json
        )
        VALUES (?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact_email,
            activity_time,
            activity_type,
            status,
            summary,
            content_snapshot,
            actor,
            outbound_log_id,
            inbound_email_id,
            delivery_event_id,
            int(remarketing_step or 0),
            template_name,
            variant_version,
            channel,
            json_dumps(metadata or {}),
        ),
    )
    activity_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return activity_id


def set_crm_contact_status(email, status, note="", actor="manual"):
    email = _clean_email(email)
    next_status = _crm_status(status)
    if not email:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM crm_contacts WHERE email = ?", (email,))
    if not cursor.fetchone():
        cursor.execute(
            """
            INSERT INTO crm_contacts (email, status, created_at, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (email, next_status),
        )
    else:
        cursor.execute(
            """
            UPDATE crm_contacts
            SET status = ?,
                next_followup_at = CASE
                    WHEN ? IN ('replied_pending_review', 'interested', 'follow_up_later', 'not_interested', 'bounced', 'abandoned')
                        THEN NULL
                    ELSE next_followup_at
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE email = ?
            """,
            (next_status, next_status, email),
        )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    if next_status in {"not_interested", "bounced"}:
        add_suppression(email, next_status)
    log_crm_activity(email, "manual_status", note or f"Status changed to {next_status}", status=next_status, actor=actor)
    return changed


def append_crm_note(email, note, actor="manual"):
    email = _clean_email(email)
    note = (note or "").strip()
    if not email or not note:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT notes FROM crm_contacts WHERE email = ?", (email,))
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            """
            INSERT INTO crm_contacts (email, notes, created_at, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (email, note),
        )
    else:
        existing = (row["notes"] or "").strip()
        next_notes = f"{existing}\n\n{note}" if existing else note
        cursor.execute(
            """
            UPDATE crm_contacts
            SET notes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE email = ?
            """,
            (next_notes, email),
        )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    log_crm_activity(email, "manual_note", note, content_snapshot=note, actor=actor)
    return changed


def set_crm_next_followup(email, next_followup_at, actor="manual"):
    email = _clean_email(email)
    if not email:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE crm_contacts
        SET next_followup_at = ?, updated_at = CURRENT_TIMESTAMP
        WHERE email = ?
        """,
        ((next_followup_at or "").strip() or None, email),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    log_crm_activity(
        email,
        "next_followup",
        f"Next follow-up set to {next_followup_at or 'blank'}",
        actor=actor,
    )
    return changed


def update_crm_channels(
    email,
    whatsapp=None,
    instagram=None,
    linkedin=None,
    external_touch_status=None,
    external_touch_channel=None,
    enrichment_status=None,
    actor="api",
):
    email = _clean_email(email)
    if not email:
        return 0
    upsert_crm_contact(email)
    updates = ["updated_at = CURRENT_TIMESTAMP"]
    params = []
    for column, value in [
        ("whatsapp", whatsapp),
        ("instagram", instagram),
        ("linkedin", linkedin),
        ("external_touch_channel", external_touch_channel),
        ("enrichment_status", enrichment_status),
    ]:
        if value is not None:
            updates.append(f"{column} = ?")
            params.append(str(value or "").strip())
    if external_touch_status is not None:
        updates.append("external_touch_status = ?")
        params.append(_crm_external_status(external_touch_status))
    params.append(email)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE crm_contacts SET {', '.join(updates)} WHERE email = ?", params)
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    log_crm_activity(
        email,
        "external_touch",
        "External channel fields updated",
        actor=actor,
        channel=(external_touch_channel or ""),
        metadata={
            "whatsapp": whatsapp,
            "instagram": instagram,
            "linkedin": linkedin,
            "external_touch_status": external_touch_status,
        },
    )
    return changed


def mark_crm_external_touch(email, status="", channel="", note="", actor="api"):
    return update_crm_channels(
        email,
        external_touch_status=status or "pending",
        external_touch_channel=channel or "",
        actor=actor,
    ) + (append_crm_note(email, note, actor=actor) if note else 0)


def mark_crm_outbound(
    email,
    outbound_log_id=0,
    sender="",
    subject="",
    body_html="",
    variant_version="",
    remarketing_step=0,
    template_name="",
    next_followup_at=None,
):
    email = _clean_email(email)
    if not email:
        return 0
    upsert_crm_contact(email)
    step = int(remarketing_step or 0)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE crm_contacts
        SET last_sent_at = CURRENT_TIMESTAMP,
            owner_sender = COALESCE(NULLIF(?, ''), owner_sender),
            remarketing_attempts = CASE
                WHEN ? > remarketing_attempts THEN ?
                ELSE remarketing_attempts
            END,
            next_followup_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE email = ?
        """,
        ((sender or "").strip().lower(), step, step, next_followup_at, email),
    )
    conn.commit()
    conn.close()
    return log_crm_activity(
        email,
        "outbound_remarketing" if step else "outbound_initial",
        subject,
        content_snapshot=body_html,
        status="sent",
        actor="dispatch",
        outbound_log_id=int(outbound_log_id or 0) or None,
        remarketing_step=step,
        template_name=template_name,
        variant_version=variant_version,
        channel="email",
    )


def mark_crm_inbound_reply(
    email,
    inbound_email_id=0,
    receiver="",
    subject="",
    content="",
    sentiment="",
    event_time=None,
    actor="imap",
):
    email = _clean_email(email)
    if not email:
        return ""
    sentiment = sentiment or "Pending"
    lowered = sentiment.lower()
    if "interested" in lowered:
        status = "interested"
    elif "refused" in lowered:
        status = "not_interested"
    elif "follow up later" in lowered:
        status = "follow_up_later"
    else:
        status = "replied_pending_review"
    upsert_crm_contact(email, status=status)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE crm_contacts
        SET status = ?,
            last_reply_at = COALESCE(?, CURRENT_TIMESTAMP),
            next_followup_at = NULL,
            owner_sender = COALESCE(NULLIF(?, ''), owner_sender),
            updated_at = CURRENT_TIMESTAMP
        WHERE email = ?
        """,
        (status, event_time, (receiver or "").strip().lower(), email),
    )
    conn.commit()
    conn.close()
    if status == "not_interested":
        add_suppression(email, "reply_refused")
    log_crm_activity(
        email,
        "inbound_reply",
        subject,
        content_snapshot=content,
        status=status,
        actor=actor,
        inbound_email_id=int(inbound_email_id or 0) or None,
        channel="email",
        activity_time=event_time,
        metadata={"sentiment": sentiment},
    )
    return status


def mark_crm_bounce(email, reason="hard_bounce", event_time=None):
    email = _clean_email(email)
    if not email:
        return 0
    upsert_crm_contact(email, status="bounced")
    add_suppression(email, reason)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE crm_contacts
        SET status = 'bounced',
            next_followup_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE email = ?
        """,
        (email,),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    log_crm_activity(email, "bounce", reason, status="bounced", actor="imap", activity_time=event_time)
    return changed


def abandon_due_crm_contacts():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT email
        FROM crm_contacts
        WHERE status = 'pending'
          AND remarketing_attempts >= max_remarketing_attempts
          AND max_remarketing_attempts > 0
          AND next_followup_at IS NOT NULL
          AND datetime(next_followup_at) <= datetime('now')
        """
    )
    emails = [row["email"] for row in cursor.fetchall()]
    if emails:
        placeholders = ",".join("?" for _ in emails)
        cursor.execute(
            f"""
            UPDATE crm_contacts
            SET status = 'abandoned',
                external_touch_status = CASE
                    WHEN external_touch_status = 'none' THEN 'pending'
                    ELSE external_touch_status
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE email IN ({placeholders})
            """,
            emails,
        )
    conn.commit()
    conn.close()
    for email in emails:
        log_crm_activity(email, "abandoned", "No reply after final remarketing step", status="abandoned", actor="system")
    return len(emails)


def crm_where_clause(filters=None):
    filters = filters or {}
    clauses = []
    params = []
    status = (filters.get("status") or "").strip().lower()
    if status in CRM_STATUSES:
        clauses.append("c.status = ?")
        params.append(status)
    external_touch_status = (filters.get("external_touch_status") or "").strip().lower()
    if external_touch_status in CRM_EXTERNAL_TOUCH_STATUSES:
        clauses.append("c.external_touch_status = ?")
        params.append(external_touch_status)
    q = (filters.get("q") or "").strip().lower()
    if q:
        clauses.append(
            """
            (
                LOWER(c.email) LIKE ?
                OR LOWER(c.name) LIKE ?
                OR LOWER(c.company) LIKE ?
                OR LOWER(c.position) LIKE ?
                OR LOWER(c.campaign) LIKE ?
                OR LOWER(c.notes) LIKE ?
            )
            """
        )
        params.extend([f"%{q}%"] * 6)
    due = (filters.get("due") or "").strip().lower()
    if due == "today":
        clauses.append("date(c.next_followup_at, 'localtime') <= date('now', 'localtime')")
    elif due == "week":
        clauses.append("date(c.next_followup_at, 'localtime') <= date('now', 'localtime', '+7 days')")
    elif due == "overdue":
        clauses.append("datetime(c.next_followup_at) < datetime('now')")
    campaign = (filters.get("campaign") or "").strip()
    if campaign:
        clauses.append("LOWER(c.campaign) LIKE ?")
        params.append(f"%{campaign.lower()}%")
    tags = _normalize_tag_names(filters.get("tags") or [])
    if tags:
        for tag in tags:
            clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM crm_contact_tags ctf
                    JOIN crm_tags tf ON tf.id = ctf.tag_id
                    WHERE ctf.contact_email = c.email
                      AND LOWER(tf.name) = LOWER(?)
                )
                """
            )
            params.append(tag)
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def list_crm_contacts(filters=None, limit=100, offset=0):
    where_sql, params = crm_where_clause(filters)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT c.*,
               GROUP_CONCAT(DISTINCT t.name) AS tag_names,
               COUNT(DISTINCT CASE WHEN tasks.status = 'open' THEN tasks.id ELSE NULL END) AS open_task_count
        FROM crm_contacts c
        LEFT JOIN crm_contact_tags ct ON ct.contact_email = c.email
        LEFT JOIN crm_tags t ON t.id = ct.tag_id
        LEFT JOIN crm_tasks tasks ON tasks.contact_email = c.email
        {where_sql}
        GROUP BY c.email
        ORDER BY
          CASE WHEN c.next_followup_at IS NULL THEN 1 ELSE 0 END,
          datetime(c.next_followup_at) ASC,
          datetime(c.updated_at) DESC
        LIMIT ? OFFSET ?
        """,
        (*params, int(limit or 100), int(offset or 0)),
    )
    rows = []
    for row in cursor.fetchall():
        item = _crm_contact_dict(row)
        item["tags"] = [tag.strip() for tag in (item.get("tag_names") or "").split(",") if tag.strip()]
        item["open_task_count"] = int(item.get("open_task_count") or 0)
        rows.append(item)
    cursor.execute(f"SELECT COUNT(*) AS count FROM crm_contacts c {where_sql}", params)
    total = int((cursor.fetchone() or {"count": 0})["count"] or 0)
    conn.close()
    return rows, total


def list_crm_contacts_for_export(filters=None):
    rows, _ = list_crm_contacts(filters=filters, limit=100000, offset=0)
    return rows


def crm_dashboard_summary():
    abandon_due_crm_contacts()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM crm_contacts
        GROUP BY status
        """
    )
    by_status = {row["status"]: int(row["count"] or 0) for row in cursor.fetchall()}
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM crm_contacts
        WHERE next_followup_at IS NOT NULL
          AND date(next_followup_at, 'localtime') <= date('now', 'localtime')
          AND status IN ('pending', 'follow_up_later')
        """
    )
    today_due = int(cursor.fetchone()["count"] or 0)
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM crm_contacts
        WHERE next_followup_at IS NOT NULL
          AND date(next_followup_at, 'localtime') <= date('now', 'localtime', '+7 days')
          AND status IN ('pending', 'follow_up_later')
        """
    )
    week_due = int(cursor.fetchone()["count"] or 0)
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM crm_contacts
        WHERE external_touch_status = 'pending'
        """
    )
    external_pending = int(cursor.fetchone()["count"] or 0)
    conn.close()
    return {
        "by_status": by_status,
        "today_due": today_due,
        "week_due": week_due,
        "external_pending": external_pending,
        "total": sum(by_status.values()),
    }


def crm_funnel_stats():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            COALESCE(remarketing_step, 0) AS step,
            COALESCE(template_name, '') AS template_name,
            COALESCE(variant_version, '') AS variant_version,
            COUNT(*) AS sent_count,
            COUNT(DISTINCT contact_email) AS contact_count
        FROM crm_activities
        WHERE activity_type IN ('outbound_initial', 'outbound_remarketing')
        GROUP BY COALESCE(remarketing_step, 0), COALESCE(template_name, ''), COALESCE(variant_version, '')
        ORDER BY step, template_name, variant_version
        """
    )
    sends = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM crm_contacts
        GROUP BY status
        """
    )
    outcomes = {row["status"]: int(row["count"] or 0) for row in cursor.fetchall()}
    cursor.execute(
        """
        SELECT
            SUM(CASE WHEN remarketing_attempts = 0 THEN 1 ELSE 0 END) AS initial_only,
            SUM(CASE WHEN remarketing_attempts >= 1 THEN 1 ELSE 0 END) AS step_1,
            SUM(CASE WHEN remarketing_attempts >= 2 THEN 1 ELSE 0 END) AS step_2,
            SUM(CASE WHEN remarketing_attempts >= 3 THEN 1 ELSE 0 END) AS step_3,
            SUM(CASE WHEN remarketing_attempts >= 4 THEN 1 ELSE 0 END) AS step_4
        FROM crm_contacts
        """
    )
    stage_row = dict(cursor.fetchone() or {})
    conn.close()
    total_replied = sum(outcomes.get(status, 0) for status in ["replied_pending_review", "interested", "follow_up_later", "not_interested"])
    return {
        "stages": {
            "initial": int(stage_row.get("initial_only") or 0),
            "remarketing_1": int(stage_row.get("step_1") or 0),
            "remarketing_2": int(stage_row.get("step_2") or 0),
            "remarketing_3": int(stage_row.get("step_3") or 0),
            "remarketing_4": int(stage_row.get("step_4") or 0),
            "interested": outcomes.get("interested", 0),
            "replied": total_replied,
            "abandoned": outcomes.get("abandoned", 0),
        },
        "by_template": sends,
        "outcomes": outcomes,
    }


def get_crm_timeline(email):
    email = _clean_email(email)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT a.*, i.subject AS inbound_subject, i.content AS inbound_content,
               i.received_at AS inbound_received_at,
               o.subject AS outbound_subject, o.body_html AS outbound_body_html,
               o.plain_text AS outbound_plain_text, o.timestamp AS outbound_timestamp,
               o.sender AS outbound_sender, o.status AS outbound_status
        FROM crm_activities a
        LEFT JOIN inbound_emails i ON i.id = a.inbound_email_id
        LEFT JOIN outbound_logs o ON o.id = a.outbound_log_id
        WHERE a.contact_email = ?
        ORDER BY datetime(a.activity_time) DESC, a.id DESC
        LIMIT 500
        """,
        (email,),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    linked_inbound = {row["inbound_email_id"] for row in rows if row.get("inbound_email_id")}
    linked_outbound = {row["outbound_log_id"] for row in rows if row.get("outbound_log_id")}
    cursor.execute(
        """
        SELECT id, received_at, sender, receiver, subject, content, sentiment
        FROM inbound_emails
        WHERE LOWER(sender) = ?
        ORDER BY datetime(received_at) DESC
        LIMIT 200
        """,
        (email,),
    )
    for row in cursor.fetchall():
        if row["id"] in linked_inbound:
            continue
        rows.append(
            {
                "id": f"inbound-{row['id']}",
                "activity_time": row["received_at"],
                "activity_type": "inbound_reply",
                "status": row["sentiment"],
                "summary": row["subject"],
                "content_snapshot": row["content"],
                "actor": "legacy_inbox",
                "inbound_email_id": row["id"],
                "channel": "email",
            }
        )
    cursor.execute(
        """
        SELECT id, timestamp, sender, receiver, subject, body_html, plain_text,
               variant_version, status, crm_remarketing_step, crm_template_name
        FROM outbound_logs
        WHERE LOWER(receiver) = ?
        ORDER BY datetime(timestamp) DESC
        LIMIT 200
        """,
        (email,),
    )
    for row in cursor.fetchall():
        if row["id"] in linked_outbound:
            continue
        rows.append(
            {
                "id": f"outbound-{row['id']}",
                "activity_time": row["timestamp"],
                "activity_type": "outbound_remarketing" if int(row["crm_remarketing_step"] or 0) else "outbound_initial",
                "status": row["status"],
                "summary": row["subject"],
                "content_snapshot": row["plain_text"] or row["body_html"],
                "actor": row["sender"],
                "outbound_log_id": row["id"],
                "remarketing_step": row["crm_remarketing_step"],
                "template_name": row["crm_template_name"],
                "variant_version": row["variant_version"],
                "channel": "email",
            }
        )
    conn.close()
    rows.sort(key=lambda item: (item.get("activity_time") or "", str(item.get("id") or "")), reverse=True)
    for row in rows:
        row["metadata"] = _safe_json_loads(row.get("metadata_json"), {})
    return rows


def list_crm_tasks(contact_email="", status="open", due=""):
    conn = get_connection()
    cursor = conn.cursor()
    clauses = []
    params = []
    if contact_email:
        clauses.append("contact_email = ?")
        params.append(_clean_email(contact_email))
    if status:
        clauses.append("status = ?")
        params.append((status or "open").strip().lower())
    if due == "today":
        clauses.append("date(due_at, 'localtime') <= date('now', 'localtime')")
    elif due == "week":
        clauses.append("date(due_at, 'localtime') <= date('now', 'localtime', '+7 days')")
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    cursor.execute(
        f"""
        SELECT *
        FROM crm_tasks
        {where_sql}
        ORDER BY
          CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
          datetime(due_at) ASC,
          datetime(updated_at) DESC
        LIMIT 500
        """,
        params,
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def upsert_crm_task(contact_email, task_type="custom", title="", due_at="", notes="", channel="", task_id=0, status="open"):
    contact_email = _clean_email(contact_email)
    if not contact_email:
        return 0
    upsert_crm_contact(contact_email)
    normalized_status = (status or "open").strip().lower()
    if normalized_status not in {"open", "done", "cancelled"}:
        normalized_status = "open"
    conn = get_connection()
    cursor = conn.cursor()
    if int(task_id or 0):
        cursor.execute(
            """
            UPDATE crm_tasks
            SET task_type = ?, title = ?, due_at = ?, status = ?, notes = ?,
                channel = ?, completed_at = CASE WHEN ? = 'done' THEN CURRENT_TIMESTAMP ELSE completed_at END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND contact_email = ?
            """,
            (
                task_type or "custom",
                title or task_type or "Follow up",
                (due_at or "").strip() or None,
                normalized_status,
                notes or "",
                channel or "",
                normalized_status,
                int(task_id or 0),
                contact_email,
            ),
        )
        task_id = int(task_id or 0)
    else:
        cursor.execute(
            """
            INSERT INTO crm_tasks (
                contact_email, task_type, title, due_at, status, notes, channel,
                completed_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'done' THEN CURRENT_TIMESTAMP ELSE NULL END, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                contact_email,
                task_type or "custom",
                title or task_type or "Follow up",
                (due_at or "").strip() or None,
                normalized_status,
                notes or "",
                channel or "",
                normalized_status,
            ),
        )
        task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    log_crm_activity(
        contact_email,
        "task",
        title or task_type or "Follow up",
        content_snapshot=notes or "",
        status=normalized_status,
        actor="manual",
        channel=channel or "",
        metadata={"task_id": task_id, "due_at": due_at, "task_type": task_type},
    )
    return task_id


def list_remarketing_templates(limit=4):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT step_number, name, subject, body, unsubscribe_copy, signature,
               cooldown_days, status, updated_at
        FROM remarketing_templates
        WHERE step_number BETWEEN 1 AND ?
        ORDER BY step_number
        """,
        (min(CRM_HARD_REMARKETING_MAX, int(limit or CRM_HARD_REMARKETING_MAX)),),
    )
    saved = {int(row["step_number"]): dict(row) for row in cursor.fetchall()}
    conn.close()
    return [
        saved.get(step)
        or {
            "step_number": step,
            "name": f"Remarketing {step}",
            "subject": "",
            "body": "",
            "unsubscribe_copy": "",
            "signature": "",
            "cooldown_days": 7,
            "status": "active",
            "updated_at": "",
        }
        for step in range(1, min(CRM_HARD_REMARKETING_MAX, int(limit or CRM_HARD_REMARKETING_MAX)) + 1)
    ]


def get_remarketing_template(step_number):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT step_number, name, subject, body, unsubscribe_copy, signature,
               cooldown_days, status, updated_at
        FROM remarketing_templates
        WHERE step_number = ?
        """,
        (max(1, min(CRM_HARD_REMARKETING_MAX, int(step_number or 1))),),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_remarketing_template(step_number, name, subject, body, unsubscribe_copy, signature, cooldown_days=7, status="active"):
    step = max(1, min(CRM_HARD_REMARKETING_MAX, int(step_number or 1)))
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO remarketing_templates (
            step_number, name, subject, body, unsubscribe_copy, signature,
            cooldown_days, status, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(step_number) DO UPDATE SET
            name = excluded.name,
            subject = excluded.subject,
            body = excluded.body,
            unsubscribe_copy = excluded.unsubscribe_copy,
            signature = excluded.signature,
            cooldown_days = excluded.cooldown_days,
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            step,
            name or f"Remarketing {step}",
            subject or "",
            body or "",
            unsubscribe_copy or "",
            signature or "",
            max(0, int(cooldown_days or 0)),
            "active" if status != "paused" else "paused",
        ),
    )
    conn.commit()
    conn.close()


def list_remarketing_candidates(limit=100):
    abandon_due_crm_contacts()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.*
        FROM crm_contacts c
        LEFT JOIN suppression_list s ON s.email = c.email
        WHERE c.status = 'pending'
          AND s.email IS NULL
          AND c.remarketing_attempts < c.max_remarketing_attempts
          AND c.max_remarketing_attempts > 0
          AND c.next_followup_at IS NOT NULL
          AND datetime(c.next_followup_at) <= datetime('now')
        ORDER BY datetime(c.next_followup_at) ASC, datetime(c.updated_at) ASC
        LIMIT ?
        """,
        (int(limit or 100),),
    )
    rows = [_crm_contact_dict(row) for row in cursor.fetchall()]
    conn.close()
    for row in rows:
        row["tags"] = get_crm_contact_tags(row["email"])
    return rows


def list_external_touch_queue(channel="", limit=100):
    channel = (channel or "").strip().lower()
    clauses = ["external_touch_status = 'pending'"]
    params = []
    if channel:
        clauses.append("(external_touch_channel = ? OR external_touch_channel = '')")
        params.append(channel)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT *
        FROM crm_contacts
        WHERE {' AND '.join(clauses)}
        ORDER BY datetime(updated_at) ASC
        LIMIT ?
        """,
        (*params, int(limit or 100)),
    )
    rows = [_crm_contact_dict(row) for row in cursor.fetchall()]
    conn.close()
    for row in rows:
        row["tags"] = get_crm_contact_tags(row["email"])
    return rows


def list_llm_settings(include_secrets=False, purpose="cold"):
    conn = get_connection()
    cursor = conn.cursor()
    purpose = purpose if purpose in LLM_PURPOSE_PROVIDERS else "cold"
    providers = tuple(sorted(LLM_PURPOSE_PROVIDERS[purpose]))
    cursor.execute(
        """
        SELECT provider, display_name, api_key_cipher, base_url, model,
               system_prompt, status, updated_at
        FROM llm_settings
        WHERE provider IN (?, ?)
        ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, provider
        """,
        providers,
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


def get_llm_settings(provider=None, purpose="cold"):
    conn = get_connection()
    cursor = conn.cursor()
    if provider:
        cursor.execute(
            """
            SELECT provider, display_name, api_key_cipher, base_url, model,
                   system_prompt, status, updated_at
            FROM llm_settings
            WHERE provider = ?
            """,
            (provider,),
        )
    else:
        purpose = purpose if purpose in LLM_PURPOSE_PROVIDERS else "cold"
        providers = tuple(sorted(LLM_PURPOSE_PROVIDERS[purpose]))
        cursor.execute(
            """
            SELECT provider, display_name, api_key_cipher, base_url, model,
                   system_prompt, status, updated_at
            FROM llm_settings
            WHERE provider IN (?, ?)
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC
            LIMIT 1
            """,
            providers,
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
    provider = (provider or "").strip().lower()
    valid_providers = LLM_PURPOSE_PROVIDERS["cold"] | LLM_PURPOSE_PROVIDERS["warm"]
    if provider not in valid_providers:
        raise ValueError("provider must be openai, anthropic, warm_openai, or warm_anthropic")

    display_name = _llm_display_name(provider)
    existing = get_llm_settings(provider)
    if api_key is None or api_key == "":
        api_key_cipher = None
    else:
        api_key_cipher = _encrypt_secret(api_key)

    conn = get_connection()
    cursor = conn.cursor()
    if status == "active":
        purpose_providers = tuple(sorted(LLM_PURPOSE_PROVIDERS[_llm_purpose(provider)]))
        cursor.execute("UPDATE llm_settings SET status = 'inactive' WHERE provider IN (?, ?) AND provider != ?", (*purpose_providers, provider))

    if existing:
        values = {
            "display_name": display_name,
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
                SET display_name = :display_name,
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
                SET display_name = :display_name,
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
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                provider,
                display_name,
                api_key_cipher or "",
                base_url,
                model,
                system_prompt,
                status,
            ),
        )
    conn.commit()
    conn.close()


def is_suppressed(email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM suppression_list WHERE email = ?", (email.strip().lower(),))
    found = cursor.fetchone() is not None
    conn.close()
    return found


def find_outbound_by_message_id(message_id):
    if not message_id:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, timestamp, sender, receiver, subject, message_id, target_domain
        FROM outbound_logs
        WHERE message_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (message_id.strip(),),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_successful_receivers(emails):
    normalized = sorted({(email or "").strip().lower() for email in emails if (email or "").strip()})
    if not normalized:
        return set()
    conn = get_connection()
    cursor = conn.cursor()
    found = set()
    for index in range(0, len(normalized), 900):
        chunk = normalized[index:index + 900]
        placeholders = ",".join("?" for _ in chunk)
        cursor.execute(
            f"""
            SELECT DISTINCT LOWER(receiver) AS receiver
            FROM outbound_logs
            WHERE status = 'success'
              AND LOWER(receiver) IN ({placeholders})
            """,
            chunk,
        )
        found.update(row["receiver"] for row in cursor.fetchall() if row["receiver"])
    conn.close()
    return found


def list_recent_successful_receivers(emails, days=7):
    normalized = sorted({(email or "").strip().lower() for email in emails if (email or "").strip()})
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        days = 7
    if not normalized or days <= 0:
        return set()
    conn = get_connection()
    cursor = conn.cursor()
    found = set()
    for index in range(0, len(normalized), 900):
        chunk = normalized[index:index + 900]
        placeholders = ",".join("?" for _ in chunk)
        cursor.execute(
            f"""
            SELECT DISTINCT LOWER(receiver) AS receiver
            FROM outbound_logs
            WHERE status = 'success'
              AND datetime(timestamp) >= datetime('now', ?)
              AND LOWER(receiver) IN ({placeholders})
            """,
            [f"-{days} days", *chunk],
        )
        found.update(row["receiver"] for row in cursor.fetchall() if row["receiver"])
    conn.close()
    return found


def log_delivery_event(
    event_type,
    sender="",
    receiver="",
    source="",
    subject="",
    message_id="",
    source_message_id="",
    target_domain="",
    severity="info",
    details="",
    event_time=None,
):
    conn = get_connection()
    cursor = conn.cursor()
    if source_message_id:
        cursor.execute(
            """
            SELECT 1 FROM delivery_events
            WHERE event_type = ?
              AND COALESCE(source_message_id, '') = ?
              AND COALESCE(receiver, '') = ?
            LIMIT 1
            """,
            (event_type, source_message_id.strip(), (receiver or "").strip().lower()),
        )
        if cursor.fetchone():
            conn.close()
            return False

    cursor.execute(
        """
        INSERT INTO delivery_events (
            event_time, sender, receiver, event_type, source, subject,
            message_id, source_message_id, target_domain, severity, details
        )
        VALUES (COALESCE(?, CURRENT_TIMESTAMP), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_time,
            (sender or "").strip().lower(),
            (receiver or "").strip().lower(),
            event_type,
            source,
            subject,
            message_id,
            source_message_id,
            (target_domain or "").strip().lower(),
            severity,
            details,
        ),
    )
    conn.commit()
    conn.close()
    return True


def get_domain_count(domain):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT sent_count FROM domain_counters WHERE domain = ? AND send_date = ?",
        (domain.lower(), _today()),
    )
    row = cursor.fetchone()
    conn.close()
    return int(row["sent_count"]) if row else 0


def increment_domain_count(domain):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO domain_counters (domain, send_date, sent_count)
        VALUES (?, ?, 1)
        ON CONFLICT(domain, send_date) DO UPDATE SET sent_count = sent_count + 1
        """,
        (domain.lower(), _today()),
    )
    conn.commit()
    conn.close()


def get_email_test_domain_count(domain):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT test_count
        FROM email_test_domain_counters
        WHERE domain = ? AND test_date = ?
        """,
        ((domain or "").strip().lower(), _today()),
    )
    row = cursor.fetchone()
    conn.close()
    return int(row["test_count"]) if row else 0


def can_run_email_test_for_domain(domain, daily_limit=3):
    domain = (domain or "").strip().lower()
    if not domain:
        return False, 0
    used = get_email_test_domain_count(domain)
    return used < int(daily_limit or 3), used


def increment_email_test_domain_count(domain):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO email_test_domain_counters (domain, test_date, test_count)
        VALUES (?, ?, 1)
        ON CONFLICT(domain, test_date) DO UPDATE SET test_count = test_count + 1
        """,
        ((domain or "").strip().lower(), _today()),
    )
    conn.commit()
    conn.close()


def log_outbound(
    sender,
    receiver,
    subject,
    body_html,
    variant,
    status,
    plain_text="",
    message_id="",
    target_domain="",
    error="",
    crm_remarketing_step=0,
    crm_template_name="",
):
    """发信内容全留底写入"""
    sender = (sender or "").strip().lower()
    receiver = (receiver or "").strip().lower()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO outbound_logs (
            sender, receiver, subject, body_html, variant_version, status,
            plain_text, message_id, target_domain, error, crm_remarketing_step,
            crm_template_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        sender,
        receiver,
        subject,
        body_html,
        variant,
        status,
        plain_text,
        message_id,
        target_domain,
        error,
        int(crm_remarketing_step or 0),
        crm_template_name or "",
    ))
    log_id = cursor.lastrowid
    if status == "success" and sender:
        _sync_sender_daily_counts_from_audit(cursor, sender)
        cursor.execute(
            """
            UPDATE senders
            SET fail_count = 0,
                last_sent_at = CURRENT_TIMESTAMP
            WHERE LOWER(email) = ?
            """,
            (sender,),
        )
    conn.commit()
    conn.close()
    return log_id


def delete_outbound_log(log_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT sender FROM outbound_logs WHERE id = ?", (int(log_id or 0),))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return 0
    sender = (row["sender"] or "").strip().lower()
    cursor.execute("DELETE FROM outbound_logs WHERE id = ?", (int(log_id or 0),))
    deleted = cursor.rowcount
    if sender:
        _sync_sender_daily_counts_from_audit(cursor, sender)
    conn.commit()
    conn.close()
    return deleted


def clear_outbound_logs():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM outbound_logs")
    deleted = cursor.rowcount
    _sync_sender_daily_counts_from_audit(cursor)
    conn.commit()
    conn.close()
    return deleted


def clear_delivery_events():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM delivery_events")
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def log_inbound(received_at, sender, receiver, subject, content, sentiment, message_id="", imap_uid="", imap_folder="INBOX"):
    conn = get_connection()
    cursor = conn.cursor()
    if message_id:
        cursor.execute("SELECT id FROM inbound_emails WHERE message_id = ?", (message_id,))
        row = cursor.fetchone()
        if row:
            conn.close()
            return False
    if imap_uid and receiver:
        cursor.execute(
            """
            SELECT id FROM inbound_emails
            WHERE receiver = ? AND imap_uid = ? AND COALESCE(imap_folder, 'INBOX') = ?
            """,
            ((receiver or "").strip().lower(), str(imap_uid), imap_folder or "INBOX"),
        )
        row = cursor.fetchone()
        if row:
            conn.close()
            return False
    cursor.execute(
        """
        INSERT INTO inbound_emails (
            received_at, sender, receiver, subject, content, sentiment,
            message_id, imap_uid, imap_folder
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            received_at,
            (sender or "").strip().lower(),
            (receiver or "").strip().lower(),
            subject,
            content,
            sentiment,
            message_id,
            imap_uid,
            imap_folder or "INBOX",
        ),
    )
    inbound_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return inbound_id


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
