# LazyLogger

Self-hosted AI agent that integrates an Obsidian vault with Telegram. Send natural language messages via Telegram and the agent reads/writes markdown files in a synced Obsidian vault using MCP tools.

## Architecture

```
┌──────────────┐     ┌──────────────────────────────────────────────┐
│  Obsidian    │     │  VPS (Docker Compose)                        │
│  (phone,     │◄───►│                                              │
│   desktop)   │Sync │  ┌──────────────────────────────────────┐   │
└──────────────┘     │  │  obsidian-sync                        │   │
                     │  │  ob sync --continuous                  │   │
                     │  └──────────┬───────────────────────────┘   │
                     │             │ shared volume: /vault          │
                     │  ┌──────────▼───────────────────────────┐   │
                     │  │  agent                                │   │
                     │  │  - LangChain + Azure OpenAI           │   │
                     │  │  - MCP tools (stdio subprocess)       │   │
                     │  │  - Telegram bot (polling)             │   │
                     │  └──────────┬───────────────────────────┘   │
                     └─────────────┼────────────────────────────────┘
                                   │ HTTPS
                     ┌─────────────▼─────────┐
                     │  Telegram              │
                     └───────────────────────┘
```

**Two Docker services:**
- **obsidian-sync** — runs `obsidian-headless` for continuous vault sync
- **agent** — Python app with LangChain agent, MCP tools (as stdio subprocess), and Telegram bot

## Prerequisites

- Docker + Docker Compose
- An Obsidian Sync subscription
- Azure OpenAI deployment (gpt-4o recommended)
- A Telegram bot token (from @BotFather)

## Quick Start

### 1. Clone and configure

```bash
git clone <repo-url> && cd LazyLogger
cp .env.example .env
# Edit .env with your credentials
```

### 2. First-time Obsidian Sync setup

```bash
# Login to Obsidian (interactive — you'll enter email/password)
docker compose run --rm obsidian-sync ob login

# List your remote vaults
docker compose run --rm obsidian-sync ob sync-list-remote

# Link the local /vault directory to your remote vault
docker compose run --rm obsidian-sync ob sync-setup --vault "Your Vault Name" --path /vault
```

After setup, extract the auth token for the `OBSIDIAN_AUTH_TOKEN` env var so subsequent starts are non-interactive.

### 3. Launch

```bash
docker compose up -d
```

### 4. Verify

Send `/start` to your bot on Telegram. Then try: "What notes do I have?"

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/today` | Show or create today's daily note |
| `/search <query>` | Search the vault |
| `/read <path>` | Read a specific note |
| `/status` | Show agent health and loaded tools |
| `/help` | List commands |

Any other text message is treated as a natural language instruction.

## Configuration

All configuration is via environment variables (`.env` file). See `.env.example` for the full list.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OBSIDIAN_AUTH_TOKEN` | Yes | — | Obsidian Sync auth token |
| `AZURE_OPENAI_ENDPOINT` | Yes | — | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_API_KEY` | Yes | — | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | No | `gpt-4o` | Model deployment name |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token |
| `TELEGRAM_AUTHORIZED_USERS` | Yes | — | Comma-separated Telegram user IDs |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `CONVERSATION_HISTORY_LIMIT` | No | `20` | Max messages kept in context |
| `LLM_TEMPERATURE` | No | `0.3` | LLM temperature |
| `LLM_MAX_TOKENS` | No | `4096` | LLM max output tokens |

## Project Structure

```
LazyLogger/
├── docker-compose.yml
├── .env.example
├── obsidian-sync/
│   ├── Dockerfile
│   └── entrypoint.sh
└── agent/
    ├── Dockerfile
    ├── pyproject.toml
    ├── system_prompt.md
    ├── src/
    │   ├── main.py          # Entry point
    │   ├── config.py         # Pydantic settings
    │   ├── agent.py          # LangChain/LangGraph agent
    │   ├── telegram_bot.py   # Telegram handlers
    │   ├── mcp_client.py     # MCP client setup
    │   └── utils.py          # Helpers
    └── tests/
```

## Security

- Only authorized Telegram user IDs can interact with the bot
- MCP server runs as a local subprocess (no network exposure)
- Vault volume is read-only for the agent container (writes go through MCP)
- All secrets in `.env` (gitignored)
