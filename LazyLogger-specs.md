# Obsidian AI Agent — Project Specification

## 1. Overview

Build a self-hosted AI agent that integrates with an Obsidian vault via Telegram. The agent receives natural language messages from Telegram, processes them through an LLM (Azure OpenAI), and reads/writes markdown files in a synced Obsidian vault using MCP (Model Context Protocol) tools. All services run in Docker Compose on an Ubuntu 24.04 VPS.

### Architecture Diagram

```
┌──────────────┐        ┌─────────────────────────────────────────────────┐
│  Obsidian    │        │  VPS (Ubuntu 24.04, 4 vCPU / 4GB RAM)          │
│  (phone,     │◄──────►│                                                 │
│   desktop)   │  Sync  │  ┌─────────────────────────────────────────┐    │
│              │        │  │  obsidian-headless (Docker)              │    │
└──────────────┘        │  │  `ob sync --continuous`                  │    │
                        │  │  ← syncs vault to/from Obsidian servers  │    │
                        │  └──────────┬──────────────────────────────┘    │
                        │             │ shared volume: /vault              │
                        │  ┌──────────▼──────────────────────────────┐    │
                        │  │  MCP Server (Docker)                     │    │
                        │  │  obsidian-mcp or filesystem MCP          │    │
                        │  │  exposes vault CRUD as MCP tools         │    │
                        │  └──────────┬──────────────────────────────┘    │
                        │             │ MCP protocol (stdio or SSE)       │
                        │  ┌──────────▼──────────────────────────────┐    │
                        │  │  AI Agent (Docker, Python)               │    │
                        │  │  - LangChain + Azure OpenAI              │    │
                        │  │  - MCP tools integration                 │    │
                        │  │  - Telegram bot (python-telegram-bot)    │    │
                        │  └──────────┬──────────────────────────────┘    │
                        │             │                                   │
                        └─────────────┼───────────────────────────────────┘
                                      │ HTTPS (Telegram Bot API)
                        ┌─────────────▼───────────┐
                        │  Telegram                │
                        │  (user sends messages)   │
                        └─────────────────────────┘
```

### Data Flow

1. User sends a message to the Telegram bot
2. The AI Agent receives the message via webhook or polling
3. The agent invokes the LLM (Azure OpenAI) with the message + MCP tools
4. The LLM decides which vault operations to perform (read/write/search)
5. The agent executes those operations via the MCP server against `/vault`
6. `obsidian-headless` detects file changes and syncs them to Obsidian's servers
7. Obsidian apps on all devices pick up the changes within seconds

---

## 2. Infrastructure & Deployment

### VPS

- **OS**: Ubuntu 24.04 LTS
- **Resources**: 4 vCPU, 4 GB RAM
- **Runtime**: Docker + Docker Compose

### Docker Compose Services

All services are defined in a single `docker-compose.yml`.

#### Service 1: `obsidian-sync`

- **Image**: Custom Dockerfile based on `node:22-slim`
- **Purpose**: Runs `obsidian-headless` in continuous sync mode
- **Install**: `npm install -g obsidian-headless`
- **Command**: `ob sync --continuous --path /vault`
- **Volume**: Named volume `vault-data` mounted at `/vault`
- **Environment variables**:
  - `OBSIDIAN_AUTH_TOKEN` — auth token for non-interactive login (obtained via `ob login` once)
- **Setup notes**:
  - On first run, you need to interactively run `ob login` and `ob sync-setup --vault "Vault Name" --path /vault` to link the local directory to the remote vault
  - After setup, continuous sync uses the stored config
  - The container should restart automatically (`restart: unless-stopped`)
- **Health check**: Verify the process is running and vault directory has recent file modifications

#### Service 2: `mcp-server`

- **Image**: Custom Dockerfile based on `node:22-slim`
- **Purpose**: Exposes the synced vault as MCP tools
- **Recommended package**: Use one of these (evaluate during implementation):
  - `@smith-and-web/obsidian-mcp-server` — Has HTTP/SSE transport, Docker support, API key auth
  - `@mauricio.wolff/mcp-obsidian` — Lightweight, 14 MCP tools, zero Obsidian plugins needed
  - `obsidian-mcp` by StevenStavrakis — Simple, well-tested
- **Volume**: Same `vault-data` volume mounted at `/vault` (read-write)
- **Transport**: SSE (Server-Sent Events) over HTTP, so the Python agent can connect to it over the network
- **Port**: Expose on internal Docker network only (e.g., `3000`)
- **Environment variables**:
  - `VAULT_PATH=/vault`
  - `API_KEY` — for authenticating requests from the agent (optional but recommended)
- **Required MCP tools** (minimum set):
  - `list_notes` / `list_directory` — list files and folders
  - `read_note` — read a note's content
  - `write_note` / `create_note` — create or overwrite a note
  - `edit_note` / `patch_note` — append or prepend content to an existing note
  - `search` — full-text search across the vault
  - `delete_note` — delete a note (available but restricted by agent policy)
  - `move_note` — move/rename a note
  - `manage_tags` / `manage_frontmatter` — if available

#### Service 3: `agent`

- **Image**: Custom Dockerfile based on `python:3.12-slim`
- **Purpose**: The AI agent — connects Telegram ↔ LLM ↔ MCP
- **Language**: Python 3.12
- **Package manager**: `uv` (fast Python package manager by Astral, replaces pip/pip-tools)
- **Dockerfile pattern**:
  ```dockerfile
  FROM python:3.12-slim
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
  WORKDIR /app
  COPY pyproject.toml uv.lock ./
  RUN uv sync --frozen --no-dev
  COPY src/ ./src/
  COPY system_prompt.md ./
  CMD ["uv", "run", "python", "-m", "src.main"]
  ```
- **Key dependencies** (in `pyproject.toml`):
  - `langchain` + `langchain-openai` — LLM orchestration with Azure OpenAI
  - `langchain-mcp-adapters` — native MCP tool integration for LangChain
  - `python-telegram-bot` — Telegram bot framework
  - `pydantic` + `pydantic-settings` — configuration management
  - `structlog` — structured logging
- **Volume**: Optionally mount `vault-data` at `/vault` as read-only for direct file access fallback
- **Environment variables** (see Section 5 for full list):
  - Azure OpenAI credentials
  - Telegram bot token
  - MCP server URL
  - Authorized Telegram user IDs
- **Networking**: Connects to `mcp-server` on the internal Docker network

#### Shared Volume

```yaml
volumes:
  vault-data:
    driver: local
```

All three services mount `vault-data`. The `obsidian-sync` and `mcp-server` services have read-write access. The `agent` service may optionally have read-only access as a fallback.

---

## 3. AI Agent Design

### Framework

- **LangChain** with **Azure OpenAI** as the LLM provider
- **langchain-mcp-adapters** for native MCP tool integration — this package allows LangChain agents to use MCP servers as tool providers without manual tool wrapping
- **Agent type**: Use LangChain's `create_agent` with the MCP tools

### MCP Integration via LangChain

Use `langchain-mcp-adapters` to connect to the MCP server:

```python
# Pseudocode — actual implementation may vary based on package version
from langchain_mcp_adapters.client import MultiServerMCPClient

async with MultiServerMCPClient(
    {
        "obsidian": {
            "url": "http://mcp-server:3000/sse",
            "transport": "sse",
        }
    }
) as client:
    tools = client.get_tools()
    agent = create_react_agent(llm, tools)
```

### LLM Configuration

- **Provider**: Azure OpenAI
- **Model**: `gpt-4o` (or configurable via env var)
- **Temperature**: 0.3 (for reliable tool use)
- **Max tokens**: 4096
- **Configuration** via `langchain-openai`:
  ```python
  from langchain_openai import AzureChatOpenAI
  llm = AzureChatOpenAI(
      azure_deployment=os.environ["AZURE_OPENAI_DEPLOYMENT"],
      azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
      api_key=os.environ["AZURE_OPENAI_API_KEY"],
      api_version=os.environ["AZURE_OPENAI_API_VERSION"],
  )
  ```

### System Prompt

The system prompt should be stored in a configurable file (`system_prompt.md`) and loaded at startup. Default content:

```markdown
You are Emanuele's personal note-taking assistant. You manage his Obsidian vault.

## Core Behavior
- You help take notes, update existing notes, search through the vault, and organize information.
- When the user says something like "add to today's notes", you should update or create today's daily note.
- Always confirm what you did after performing an action (e.g., "I've added that to today's daily note.").
- Be concise in responses — this is a Telegram chat, not an essay.

## Daily Notes
- Daily notes live in a folder structure: `YYYY/MM/YYYYMMMDD.md`
  - Example: `2026/03/2026Mar02.md`
  - The month folder uses two-digit month numbers: `01`, `02`, ..., `12`
  - The filename uses three-letter month abbreviation: `Jan`, `Feb`, `Mar`, `Apr`, `May`, `Jun`, `Jul`, `Aug`, `Sep`, `Oct`, `Nov`, `Dec`
- When creating a new daily note, use this template:
  ```
  # YYYY-MM-DD — Day of Week
  
  ## Notes
  
  ## Tasks
  
  ## Ideas
  ```
- When appending to today's note, add content under the appropriate section.
- If unsure which section, add under `## Notes`.

## Safety Rules
- **NEVER delete any note.** If asked to delete, explain that deletion is disabled for safety and suggest archiving instead.
- **NEVER overwrite a note's entire content.** Always read first, then append or edit specific sections.
- Before making destructive edits, read the current content and confirm with the user.
- Always create parent folders if they don't exist when creating a new note.

## Formatting
- Use standard Obsidian-compatible Markdown.
- Use `[[wikilinks]]` for internal links when referencing other notes.
- Use tags in the format `#tag` when appropriate.
- Use frontmatter (YAML) at the top of notes when creating structured content.
```

> **Note**: This prompt is a starting point. Emanuele will tweak it to his preferences.

### Conversation Memory

- Use **LangChain's conversation buffer memory** scoped per Telegram chat
- Keep the last **20 messages** in context (configurable)
- Memory is in-process only (resets on container restart) — this is acceptable for v1
- Future improvement: persist conversation history to a file or Redis

### Error Handling

- If the MCP server is unreachable, respond to the user: "I can't access the vault right now. I'll try again shortly."
- If the LLM call fails, respond: "I'm having trouble thinking right now. Please try again in a moment."
- Log all errors with full context using `structlog`
- Implement retry logic with exponential backoff for transient failures (MCP, LLM)

---

## 4. Telegram Bot

### Library

- `python-telegram-bot` (v21+, async)

### Features

- **Polling mode** for v1 (simpler, no need to set up webhooks/SSL)
- Future improvement: switch to webhook mode behind a reverse proxy for lower latency
- **Authorized users only**: Check `message.from_user.id` against a configurable allowlist
- Unauthorized users receive: "Sorry, I'm not available for public use."

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message explaining capabilities |
| `/today` | Show or create today's daily note |
| `/search <query>` | Search the vault for a term |
| `/read <path>` | Read a specific note |
| `/status` | Show sync status and agent health |
| `/help` | List available commands |

### Natural Language Handling

Any message that isn't a command is treated as a natural language instruction passed to the LangChain agent. Examples:

- "Add to today's notes: had a meeting with Silvia about Powsh funding round"
- "What did I write about SOFIA last week?"
- "Create a new note called Projects/VillaDesign with sections for Kitchen, Living Room, and Bedroom"
- "Append a task to today's note: call the architect about permits"

### Message Handling Flow

```python
async def handle_message(update, context):
    user_id = update.message.from_user.id
    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("Sorry, I'm not available for public use.")
        return

    user_message = update.message.text
    # Send "typing" indicator
    await update.message.chat.send_action("typing")
    
    # Invoke the LangChain agent
    response = await agent.ainvoke({"input": user_message})
    
    # Send response (split if > 4096 chars for Telegram limit)
    await send_long_message(update, response["output"])
```

### Telegram Message Length

Telegram has a 4096-character limit per message. Implement a helper that splits long responses into multiple messages at paragraph boundaries.

---

## 5. Configuration

All configuration via environment variables, managed through a `.env` file (not committed to git).

```env
# === Obsidian Sync ===
OBSIDIAN_AUTH_TOKEN=<token-from-ob-login>

# === Azure OpenAI ===
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2024-12-01-preview

# === Telegram ===
TELEGRAM_BOT_TOKEN=<token-from-botfather>
TELEGRAM_AUTHORIZED_USERS=123456789  # comma-separated Telegram user IDs

# === MCP Server ===
MCP_SERVER_URL=http://mcp-server:3000/sse
MCP_API_KEY=<generated-secret>

# === Agent ===
SYSTEM_PROMPT_PATH=/app/system_prompt.md
CONVERSATION_HISTORY_LIMIT=20
LLM_TEMPERATURE=0.3
LLM_MAX_TOKENS=4096
LOG_LEVEL=INFO
```

Use `pydantic-settings` to load and validate these:

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Azure OpenAI
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_api_version: str = "2024-12-01-preview"
    
    # Telegram
    telegram_bot_token: str
    telegram_authorized_users: list[int]  # parsed from comma-separated
    
    # MCP
    mcp_server_url: str = "http://mcp-server:3000/sse"
    mcp_api_key: str = ""
    
    # Agent
    system_prompt_path: str = "/app/system_prompt.md"
    conversation_history_limit: int = 20
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
```

---

## 6. Project Structure

```
obsidian-agent/
├── docker-compose.yml
├── .env                          # NOT committed to git
├── .env.example                  # Template with placeholder values
├── .gitignore
├── README.md
│
├── obsidian-sync/
│   ├── Dockerfile                # node:22-slim + obsidian-headless
│   └── entrypoint.sh             # Runs ob sync --continuous
│
├── mcp-server/
│   ├── Dockerfile                # node:22-slim + chosen MCP package
│   └── entrypoint.sh
│
├── agent/
│   ├── Dockerfile                # python:3.12-slim + uv
│   ├── pyproject.toml            # Dependencies managed by uv
│   ├── uv.lock                   # Lockfile (committed to git)
│   ├── system_prompt.md          # Editable system prompt
│   │
│   ├── src/
│   │   ├── __init__.py
│   │   ├── main.py               # Entry point: starts Telegram bot + agent
│   │   ├── config.py             # Pydantic Settings
│   │   ├── agent.py              # LangChain agent setup with MCP tools
│   │   ├── telegram_bot.py       # Telegram bot handlers
│   │   ├── mcp_client.py         # MCP client connection management
│   │   └── utils.py              # Helpers (message splitting, date utils, etc.)
│   │
│   └── tests/
│       ├── __init__.py
│       ├── test_agent.py
│       ├── test_config.py
│       └── test_utils.py
│
└── scripts/
    ├── setup.sh                  # First-time setup script
    └── backup.sh                 # Vault backup script
```

---

## 7. Docker Compose

```yaml
version: "3.8"

services:
  obsidian-sync:
    build: ./obsidian-sync
    container_name: obsidian-sync
    restart: unless-stopped
    volumes:
      - vault-data:/vault
    environment:
      - OBSIDIAN_AUTH_TOKEN=${OBSIDIAN_AUTH_TOKEN}
    healthcheck:
      test: ["CMD", "pgrep", "-f", "ob sync"]
      interval: 30s
      timeout: 10s
      retries: 3

  mcp-server:
    build: ./mcp-server
    container_name: mcp-server
    restart: unless-stopped
    volumes:
      - vault-data:/vault:rw
    environment:
      - VAULT_PATH=/vault
      - API_KEY=${MCP_API_KEY}
      - PORT=3000
    ports:
      - "127.0.0.1:3000:3000"  # Expose only on localhost
    depends_on:
      obsidian-sync:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  agent:
    build: ./agent
    container_name: obsidian-agent
    restart: unless-stopped
    volumes:
      - vault-data:/vault:ro   # Read-only fallback access
    env_file:
      - .env
    depends_on:
      mcp-server:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import requests; requests.get('http://localhost:8080/health')"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  vault-data:
```

---

## 8. First-Time Setup Procedure

1. **Provision the VPS** and install Docker + Docker Compose
2. **Create the Telegram bot**:
   - Message `@BotFather` on Telegram
   - `/newbot` → choose name and username
   - Save the bot token
   - Get your Telegram user ID (message `@userinfobot`)
3. **Get Obsidian auth token**:
   - On the VPS (or locally), run:
     ```bash
     npx obsidian-headless login
     ```
   - Then set up the vault link:
     ```bash
     mkdir -p /data/vault
     cd /data/vault
     npx obsidian-headless sync-setup --vault "Your Vault Name"
     ```
   - Extract the auth token for the `OBSIDIAN_AUTH_TOKEN` env var
4. **Prepare Azure OpenAI**:
   - Ensure you have a deployment (e.g., `gpt-4o`) in your Azure OpenAI resource
   - Collect endpoint, API key, deployment name, API version
5. **Configure `.env`**:
   - Copy `.env.example` to `.env`
   - Fill in all values
6. **Launch**:
   ```bash
   docker compose up -d
   ```
7. **Verify**:
   - Send `/start` to your bot on Telegram
   - Send "What notes do I have?" to verify vault access
   - Create a test note and check it appears on your devices

---

## 9. Security Considerations

- **Telegram user allowlist**: Only configured user IDs can interact with the bot. All other messages are rejected.
- **MCP API key**: The MCP server requires an API key for all requests. The agent includes this in its MCP client configuration.
- **No public ports**: The MCP server is only exposed on `127.0.0.1` and the Docker internal network. It is not accessible from the internet.
- **Vault volume**: The agent has read-only access to the vault volume as a fallback. All writes go through MCP.
- **Secrets management**: All secrets live in `.env` which is `.gitignore`d. Never hardcode secrets.
- **Obsidian Sync encryption**: If you use end-to-end encryption on your Obsidian vault, the headless client supports it — you'll need to provide the E2EE password during setup.

---

## 10. Future Improvements

These are out of scope for v1 but worth considering:

- [ ] **Webhook mode** for Telegram (lower latency, requires HTTPS + domain)
- [ ] **Persistent conversation memory** (Redis or SQLite in a volume)
- [ ] **Voice message support** — transcribe Telegram voice messages via Azure Speech or Whisper, then process as text
- [ ] **Scheduled tasks** — daily summary generation, weekly review notes
- [ ] **Multi-user support** — each authorized user gets their own conversation context
- [ ] **Monitoring** — Prometheus metrics, health dashboard
- [ ] **Backup** — periodic `tar` of the vault volume to Azure Blob Storage
- [ ] **Claude API as alternative LLM** — make the LLM provider swappable via config
- [ ] **Image support** — process images sent via Telegram (receipts, whiteboards) and attach to notes
- [ ] **OpenClaw migration** — if the agent grows complex enough, consider migrating to OpenClaw for its full agent framework, persistent memory, and multi-channel support

---

## 11. Dependencies & Versions

### Python (agent)

| Package | Version | Purpose |
|---------|---------|---------|
| `langchain` | >=0.3 | Agent orchestration |
| `langchain-openai` | >=0.3 | Azure OpenAI LLM provider |
| `langchain-mcp-adapters` | latest | MCP tool integration for LangChain |
| `python-telegram-bot` | >=21.0 | Telegram bot framework (async) |
| `pydantic` | >=2.0 | Data validation |
| `pydantic-settings` | >=2.0 | Environment configuration |
| `structlog` | >=24.0 | Structured logging |
| `httpx` | latest | Async HTTP client (for MCP SSE) |

### Node.js (obsidian-sync, mcp-server)

| Package | Version | Purpose |
|---------|---------|---------|
| `obsidian-headless` | latest | Official Obsidian Sync headless client |
| MCP server package | latest | Vault MCP tools (choose during implementation) |

### System

| Component | Version |
|-----------|---------|
| Docker | 27+ |
| Docker Compose | 2.x |
| Node.js | 22+ (in containers) |
| Python | 3.12 (in container) |
| uv | latest (in agent container) |
| Ubuntu | 24.04 LTS (host) |
