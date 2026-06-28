# manage

`manage` 是 `retoolautoregautomange` 项目中的账号管理与网关子系统。

它的职责聚焦在：

- 账号库存管理
- Retool 组织会话池管理
- bundle 导入/导出
- OpenAI 兼容网关
- 内置管理页面

## Why

- Retool workspaces are isolated by org.
- Browser login state expires and must be refreshed by operators.
- Internal users want one stable API endpoint instead of managing many org sessions.

Inside the full repository, this submodule is the management and serving layer:

- Upstream registration/collection layer
  - root-level scripts register accounts and collect login sessions
- `manage/` layer
  - loads managed org sessions
  - exposes admin UI and OpenAI-compatible APIs

## Features

- OpenAI-compatible endpoints:
  - `/v1/models`
  - `/v1/chat/completions`
  - `/v1/responses`
- Claude Code compatible endpoint:
  - `/v1/messages`
- Managed org pool with health refresh and cooldown handling
- File-based API key registry with admin/inference scopes
- Built-in admin page for:
  - API key management
  - org health overview
  - account inventory management
  - session bundle imports
  - refresh job execution
- Session-pool tooling for:
  - bundle import/export
  - account refresh job execution

## Project Status

Current release is a practical first open-source cut focused on single-instance internal deployments.

Included:

- local runtime
- file-backed state
- admin page
- bundle import/export workflow
- desktop browser collection scripts

Not included:

- database-backed control plane
- automatic token renewal
- distributed multi-instance coordination
- container/service deployment templates

## Architecture

```text
Root registration scripts
  -> register / collect / refresh accounts
  -> export session_bundle.json

manage gateway
  -> import bundle into orgs.json
  -> maintain org health and routing state
  -> expose OpenAI-compatible APIs

Internal clients
  -> Codex / Claude Code / custom tools
```

## Quick Start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Prepare config

`gateway_config.json`

```json
{
  "conversation_header": "X-Conversation-ID",
  "timezone": "Asia/Shanghai",
  "orgs_file": "orgs.json",
  "allow_empty_org_pool": true,
  "request_timeout_seconds": 120,
  "poll_interval_seconds": 1.0,
  "poll_max_attempts": 300,
  "mapping_ttl_seconds": 604800,
  "health_cooldown_seconds": 300,
  "health_refresh_interval_seconds": 300,
  "admin_warning_days": 2,
  "audit_history_limit": 500,
  "models": [
    {
      "id": "gpt-5.5",
      "owned_by": "openai",
      "agent_name": "gpt5",
      "model_name": "gpt-5.5",
      "display_name": "Retool pooled GPT-5.5"
    },
    {
      "id": "claude-sonnet-4-6",
      "owned_by": "anthropic",
      "agent_name": "claude",
      "model_name": "claude-sonnet-4-6",
      "display_name": "Retool pooled Claude Sonnet 4.6"
    }
  ]
}
```

`orgs.json`

```json
[
  {
    "id": "office-org-001",
    "domain_name": "example.retool.com",
    "x_xsrf_token": "your-xsrf-token",
    "accessToken": "your-access-token",
    "enabled": true
  }
]
```

`api_keys.json`

```json
[
  {
    "id": "office-default",
    "key": "sk-internal-example",
    "enabled": true,
    "owner": "office",
    "scopes": ["inference", "admin"]
  }
]
```

### 3. Run

Windows helper:

```bat
run_gateway_local.bat
```

Manual:

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### 4. Open admin page

```text
http://127.0.0.1:8000/admin
```

## Session Collection Workflow

### Import org credentials from CSV

```bash
python scripts/import_orgs_csv.py --csv orgs_import_template.csv --gateway-config gateway_config.json
```

### Refresh sessions from account inventory

```bash
python scripts/build_org_sessions_from_accounts.py \
  --accounts-csv accounts_import_template.csv \
  --gateway-config gateway_config.json \
  --bundle-output runtime/session_bundle.json \
  --browser-provider cloakbrowser \
  --headless \
  --check-model gpt-5.5 \
  --check-model claude-sonnet-4-6
```

Windows helper:

```bat
run_collect_sessions_geekez.bat
```

### Import session bundle into gateway

```bash
python scripts/import_session_bundle.py \
  --bundle runtime/session_bundle.json \
  --gateway-config gateway_config.json
```

Windows helper:

```bat
run_import_session_bundle.bat
```

## API Compatibility

| Method | Path | Purpose |
|------|------|------|
| `GET` | `/models` | Anonymous model list |
| `GET` | `/v1/models` | Authenticated model list |
| `POST` | `/v1/chat/completions` | OpenAI chat completions |
| `POST` | `/v1/responses` | OpenAI responses |
| `POST` | `/v1/messages` | Claude Code compatible entry |
| `GET` | `/healthz` | Health check |
| `GET` | `/admin` | Admin UI |

## Admin Panel

The built-in admin UI currently supports:

- viewing org health and cooldown state
- importing session bundles
- managing API keys
- managing account inventory
- running account refresh jobs
- reviewing audit history

## Security Notes

Do not commit real runtime files:

- `orgs.json`
- `api_keys.json`
- `accounts.json`
- `client_api_keys.json`
- `runtime/`
- `session_bundle*.json`
- `signup_accounts.csv`

Only keep desensitized examples in:

- `api_keys.example.json`
- `orgs.example.json`
- `accounts_import_template.csv`
- `orgs_import_template.csv`

## Limitations

- file-backed state only
- no automatic token renewal
- no multi-instance coordination
- control-plane collection still depends on a real browser environment
- very long conversations remain constrained by Retool upstream limits

## Roadmap

- better release packaging
- cleaner bootstrap for Linux serving nodes
- optional database-backed state
- safer operator workflows for account refresh
- richer health and usage visibility

## Community

This project is being prepared for public release and community sharing.

If you publish it to LINUX DO:

- keep the full source open
- keep the community attribution visible
- keep AI-assisted project-introduction disclosures consistent with the post requirements

## License

Choose and add your final open-source license before publishing.
