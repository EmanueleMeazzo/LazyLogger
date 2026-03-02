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

- Docker + Docker Compose (or Podman)
- An [Obsidian Sync](https://obsidian.md/sync) subscription
- An Azure OpenAI deployment (gpt-4o recommended)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))

## Quick Start

### 1. Clone and configure

```bash
git clone <repo-url> && cd LazyLogger
cp .env.example .env
```

Edit `.env` with your credentials (see [Configuration](#configuration) below).

### 2. First-time Obsidian Sync setup

The obsidian-sync container needs a one-time interactive login to authenticate with Obsidian's servers. The credentials are persisted in a Docker volume (`obsidian-config`) so you only do this once.

```bash
# Step 1: Login to your Obsidian account (interactive — enter email/password)
docker compose run --rm obsidian-sync ob login

# Step 2: List your remote vaults to find the exact name
docker compose run --rm obsidian-sync ob sync-list-remote

# Step 3: Link the container's /vault directory to your remote vault
docker compose run --rm obsidian-sync ob sync-setup --vault "Your Vault Name" --path /vault
```

> **Note:** If you get "permission denied" on the Docker socket, either prefix
> commands with `sudo` or add your user to the docker group:
> ```bash
> sudo usermod -aG docker $USER
> # Log out and back in for this to take effect
> ```

### 3. Launch

```bash
docker compose up -d --build
```

### 4. Verify

Check the logs to make sure both services are healthy:

```bash
# All services
docker compose ps

# Follow agent logs
docker compose logs -f agent

# Follow sync logs
docker compose logs -f obsidian-sync
```

Then send `/start` to your bot on Telegram. Try: "Create a note for today: hello world!"

### 5. Updating

After pulling code changes:

```bash
git pull
docker compose up -d --build
```

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
| `AZURE_OPENAI_ENDPOINT` | Yes | — | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_API_KEY` | Yes | — | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | No | `gpt-4o` | Model deployment name |
| `AZURE_OPENAI_API_VERSION` | No | `2025-03-01-preview` | API version override |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token from @BotFather |
| `TELEGRAM_AUTHORIZED_USERS` | Yes | — | Comma-separated Telegram usernames (without @) |
| `LOG_LEVEL` | No | `INFO` | Logging level (`DEBUG` for verbose agent tracing) |
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

- Only authorized Telegram usernames can interact with the bot
- MCP server runs as a local subprocess (no network exposure)
- All secrets in `.env` (gitignored)

## Troubleshooting

**Agent crashes with `SettingsError`**: Check your `.env` — all required variables must be set. `TELEGRAM_AUTHORIZED_USERS` should be plain usernames (e.g., `alice,bob`), not JSON.

**obsidian-sync unhealthy**: Run `docker compose logs obsidian-sync` — likely needs `ob login` (see step 2 above). The healthcheck waits up to 60s for the first sync to create `/vault/.obsidian`.

**Agent returns "I'm having trouble thinking"**: Set `LOG_LEVEL=DEBUG` and check `docker compose logs -f agent` to see tool calls and LLM responses.

**Azure OpenAI 404**: The `AZURE_OPENAI_DEPLOYMENT` must match the exact deployment name in your Azure portal (not the model name).
