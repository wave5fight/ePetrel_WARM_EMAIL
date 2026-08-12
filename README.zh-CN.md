# MutualWarm

MutualWarm 是 ePetrel MutualWarm Network 的开源本地客户端。它可以帮助已获得授权的 Gmail 和 Google Workspace 邮箱加入私有预热集群、验证邮箱所有权、交换低频且自然的预热对话，并将投递结果和任务结果报告给 ePetrel 调度器。

本仓库包含本地 Web 控制台和工作进程，不包含远程 ePetrel BFF、账户系统或生产环境调度器。创建集群、审批成员、领取任务以及上报结果时，仍然需要有效的 ePetrel Warm 授权。

## 功能

| 功能领域 | 功能说明 |
| --- | --- |
| 预热网络 | 创建或加入私有预热集群、审批成员并同步集群状态 |
| 邮箱配置 | 保存用于 Warm 的 Gmail / Google Workspace OAuth 邮箱 |
| Gmail API | 通过 OAuth 连接 Gmail 或 Google Workspace 发件邮箱，支持发送、扫描、回复和收件箱救援 |
| 所有权检查 | 通过 ePetrel BFF 探测验证预热邮箱所有权 |
| 本地工作进程 | 领取调度任务、发送初始预热邮件、扫描投递位置、救援 Gmail 垃圾邮件中的支持场景，并发送延迟回复 |
| 预热内容 | 使用兼容 OpenAI API 的模型生成简短、自然的预热对话 |
| 本地存储 | 使用 SQLite 保存发件邮箱、预热状态、加密密钥、任务日志和内容指纹 |

## 重要说明

- MutualWarm 不保证邮件一定进入收件箱。邮箱信誉、身份认证、历史记录、用户行为、服务商政策和内容质量都会影响投递结果。
- 仅使用您本人拥有或获授权运营的邮箱。
- 开源客户端依赖 ePetrel BFF 提供授权、集群状态、调度策略和任务分配能力。
- 预热内容必须保持低风险、非推广性质。不得将预热邮件用于销售触达、欺骗或规避垃圾邮件过滤。

## 技术栈

- 后端：FastAPI 和 Uvicorn
- 用户界面：Jinja2 模板和静态 CSS
- 数据：SQLite
- 邮件：Gmail API OAuth
- AI：兼容 OpenAI API 的 Chat Completions 接口

## 项目结构

```text
MutualWarm/
├── web_app.py                  # FastAPI 本地 Web 控制台
├── config.py                   # 环境变量和默认配置
├── requirements.txt            # Python 依赖
├── templates/
│   ├── base.html               # 共享布局
│   ├── warm.html               # MutualWarm Network 首页
│   └── config.html             # 发件邮箱和 Warm LLM 配置
├── static/                     # CSS 资源
├── database/
│   └── db_manager.py           # SQLite 结构、迁移和数据访问
└── modules/
    ├── warm_service.py         # ePetrel BFF Warm API 客户端
    ├── warm_worker.py          # 本地预热任务工作进程
    ├── warm_client.py          # 集群密钥、策略辅助函数和服务商检测
    ├── warm_content.py         # 预热对话生成和备用模板
    ├── warm_account_probe.py   # 所有权探测扫描和收件箱救援
    ├── gmail_api.py            # Gmail OAuth 和 Gmail API 辅助函数
    ├── email_utils.py          # 邮箱地址规范化辅助函数
    ├── llm_client.py           # Warm LLM 客户端封装
    └── safe_logging.py         # 安全记录日志的辅助函数
```

## 安装

建议使用 Python 3.10 或更高版本。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 配置

在项目根目录创建 `.env` 文件，可参考 `.env.example`。

最小可用配置如下：

```bash
EPETREL_SESSION_SECRET="change-this-local-session-secret"
EPETREL_DB_PATH="database/storage.db"

MAIL_FROM_NAME="MutualWarm"

OPENAI_API_KEY=""
OPENAI_BASE_URL="https://api.openai.com/v1"
OPENAI_MODEL="gpt-4o-mini"
```

您也可以在 `Configuration` 页面中保存 Gmail OAuth 邮箱，以及 Warm 兼容 OpenAI API 的 LLM 配置。

## 启动

```bash
uvicorn web_app:app --host 127.0.0.1 --port 8000
```

然后打开：

```text
http://127.0.0.1:8000
```

## 基本工作流程

1. 打开 `Configuration` 页面。
2. 添加至少一个 Gmail 或 Google Workspace 发件邮箱。
3. 如需使用全自动预热，为该邮箱连接 Gmail API OAuth。
4. 保存 Warm 兼容 OpenAI API 的 LLM 配置。
5. 打开 `MutualWarm Network` 页面。
6. 登录 ePetrel。
7. 创建私有预热集群，或使用邀请加入已有集群。
8. 启用已验证的预热邮箱，并保持本地工作进程运行。

## Gmail API OAuth

MutualWarm 会申请全自动预热所需的 Gmail 权限范围，包括发送、扫描、回复以及支持的收件箱救援操作。请配置您自己的 Google Cloud OAuth 客户端，然后在 `Configuration` 页面为每个发件邮箱填写客户端 ID 和客户端密钥，再连接 Gmail API。


## 路由

| 路由 | 用途 |
| --- | --- |
| `GET /` | MutualWarm Network 首页 |
| `GET /warm` | MutualWarm Network 首页 |
| `GET /config` | Gmail OAuth 邮箱和 Warm LLM 配置 |
| `POST /senders*` | 管理 Gmail API 邮箱池 |
| `POST /gmail/oauth/start` 和 `GET /gmail/oauth/callback` | Gmail API OAuth |
| `POST /llm` | Warm 兼容 OpenAI API 的 LLM 配置 |
| `GET/POST /warm/*` | Warm 授权、集群、成员、邮箱、所有权和内容预览 |

这个独立客户端仅包含 MutualWarm 网络、Gmail OAuth 邮箱、Warm LLM、代理设置和本地工作进程功能。
