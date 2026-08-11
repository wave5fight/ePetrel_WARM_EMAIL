# MutualWarm

MutualWarm is an open-source local client for the ePetrel MutualWarm Network. It helps opted-in Gmail and Google Workspace mailboxes join private warm clusters, verify mailbox ownership, exchange natural low-volume warm conversations, and report placement/task results to the ePetrel scheduler.

This repository contains the local web console and worker. It does not include the remote ePetrel BFF, account system, or production scheduler. A valid ePetrel Warm authorization is still required for cluster creation, member approval, task claiming, and reporting.

## Features

| Area | Features |
| --- | --- |
| Warm network | Create or join private warm clusters, approve members, and sync cluster state |
| Mailbox setup | Manage sender mailboxes manually or by CSV/XLSX import |
| Gmail API | Connect Gmail or Google Workspace senders with OAuth for send, scan, reply, and inbox rescue capabilities |
| Ownership checks | Verify warm mailbox ownership through ePetrel BFF probes |
| Local worker | Claim scheduler tasks, send initial warm messages, scan placement, rescue supported Gmail spam placements, and send delayed replies |
| Warm content | Generate short natural warm conversations with an OpenAI-compatible model |
| Local storage | Store senders, warm state, encrypted secrets, task logs, and content fingerprints in SQLite |

## Important Notes

- MutualWarm does not guarantee inbox placement. Mailbox reputation, authentication, history, user behavior, provider policy, and content quality all matter.
- Use only mailboxes that you own or are authorized to operate.
- The open-source client depends on the ePetrel BFF for authorization, cluster state, scheduler policy, and task assignment.
- Warm content must remain low-stakes and non-promotional. Do not use warm messages for sales outreach, deception, or spam-filter evasion.

## Tech Stack

- Backend: FastAPI and Uvicorn
- UI: Jinja2 templates and static CSS
- Data: SQLite
- Email: SMTP/IMAP plus Gmail API OAuth
- AI: OpenAI-compatible Chat Completions
- Files: CSV/XLSX sender import through pandas and openpyxl

## Project Structure

```text
MutualWarm/
├── web_app.py                  # FastAPI local web console
├── config.py                   # Environment variables and defaults
├── requirements.txt            # Python dependencies
├── templates/
│   ├── base.html               # Shared layout
│   ├── warm.html               # MutualWarm Network home
│   └── config.html             # Sender and Warm LLM configuration
├── static/                     # CSS and downloadable sender template assets
├── database/
│   └── db_manager.py           # SQLite schema, migrations, and data access
└── modules/
    ├── warm_service.py         # ePetrel BFF Warm API client
    ├── warm_worker.py          # Local warm task worker
    ├── warm_client.py          # Cluster keys, policy helpers, and provider detection
    ├── warm_content.py         # Warm conversation generation and fallback templates
    ├── warm_account_probe.py   # Ownership probe scanning and inbox rescue
    ├── gmail_api.py            # Gmail OAuth and Gmail API helpers
    ├── sender_checks.py        # SMTP/IMAP login checks
    ├── email_engine.py         # Shared sender normalization and send helpers
    └── safe_logging.py         # Secret-safe logging helpers
```

## Install

Python 3.10 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure

Create a `.env` file in the project root. Start from `.env.example`.

Minimum useful configuration:

```bash
EPETREL_SESSION_SECRET="change-this-local-session-secret"
EPETREL_DB_PATH="database/storage.db"

MAIL_FROM_NAME="MutualWarm"
MAILFORGE_SMTP_HOST=""
MAILFORGE_SMTP_PORT=587
MAILFORGE_IMAP_HOST=""
MAILFORGE_IMAP_PORT=993

OPENAI_API_KEY=""
OPENAI_BASE_URL="https://api.openai.com/v1"
OPENAI_MODEL="gpt-4o-mini"
```

You can also save sender mailboxes and the Warm OpenAI-compatible LLM settings from the `Configuration` page.

## Start

```bash
uvicorn web_app:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

## Basic Workflow

1. Open `Configuration`.
2. Add at least one Gmail or Google Workspace sender.
3. For full-auto warm, connect Gmail API OAuth for that sender.
4. Save the Warm OpenAI-compatible LLM configuration.
5. Open `MutualWarm Network`.
6. Log in to ePetrel.
7. Create a private warm cluster or join one with an invite.
8. Enable verified warm mailboxes and keep the local worker running.

## Gmail API OAuth

MutualWarm requests Gmail scopes needed for full-auto warm behavior, including sending, scanning, replying, and supported inbox rescue actions. Configure your own Google Cloud OAuth client, then enter the client ID and client secret for each sender on the `Configuration` page before connecting Gmail API.

Use an app password only for SMTP/IMAP senders. Do not enter your main Google or Microsoft login password.

## Routes

| Route | Purpose |
| --- | --- |
| `GET /` | MutualWarm Network home |
| `GET /warm` | Legacy redirect to `/` |
| `GET /config` | Sender, Gmail OAuth, and Warm LLM configuration |
| `POST /senders*` | Sender pool management |
| `POST /gmail/oauth/start` and `GET /gmail/oauth/callback` | Gmail API OAuth |
| `POST /llm` | Warm OpenAI-compatible LLM settings |
| `GET/POST /warm/*` | Warm auth, clusters, members, mailboxes, ownership, and content preview |

Legacy cold email, CRM, inbox, audit, security, seed, lead, and email-test pages are not exposed by the open-source MutualWarm client.
