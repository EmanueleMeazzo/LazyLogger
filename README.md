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
- An Azure OpenAI deployment (gpt-5 recommended)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))

Tested with GPT-5.

## Quick Start

### 1. Clone and configure

```bash
git clone <repo-url> && cd LazyLogger
cp .env.example .env
```

Edit `.env` with your credentials (see [Configuration](#configuration) below).

### 2. Create a Telegram bot

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts to choose a name and username
3. BotFather will reply with a **bot token** — copy it into your `.env` as `TELEGRAM_BOT_TOKEN`
4. (Optional) Send `/setprivacy` → select your bot → **Disable** if you want the bot to see all messages in groups

> For a detailed walkthrough, see the [Telegram Bot API docs](https://core.telegram.org/bots/tutorial).

### 3. First-time Obsidian Sync setup

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

### 4. Launch

```bash
docker compose up -d --build
```

### 5. Verify

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

### 6. Updating

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
Voice notes and audio files are transcribed with Azure OpenAI Whisper and then processed as natural language.
Document attachments (for example PDF and office files) are saved into the vault under `Attachments/YYYY/MM/` and linked from today's daily note in an `## Attachments` section.
Photo messages are also saved into the vault under `Attachments/YYYY/MM/`, parsed by the multimodal model, and summarized into today's daily note (with the photo link under `## Attachments`).

Default natural-language behavior:
- Messages containing one or more URLs are automatically parsed via Crawl4AI and stored as dedicated link notes with backlinks in today's note.
- Messages that are not direct questions/requests are stored as memory entries in today's daily note.
- Transcribed audio is prefixed with `[Transcribed audio]` before normal routing.

## Configuration

All configuration is via environment variables (`.env` file). See `.env.example` for the full list.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AZURE_OPENAI_ENDPOINT` | Yes | — | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_API_KEY` | Yes | — | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT` | No | `gpt-5` | Model deployment name |
| `AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT` | No | `whisper-1` | Whisper transcription deployment name used for audio messages |
| `AZURE_OPENAI_API_VERSION` | No | `2025-03-01-preview` | API version override |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token from @BotFather |
| `TELEGRAM_AUTHORIZED_USERS` | Yes | — | Comma-separated Telegram usernames (without @) |
| `LOG_LEVEL` | No | `INFO` | Logging level (`DEBUG` for verbose agent tracing) |
| `LLM_MAX_TOKENS` | No | `4096` | LLM max output tokens |
| `URL_EXTRACTION_ENABLED` | No | `true` | Enables automatic URL processing in normal chat messages |
| `URL_EXTRACTOR_BACKEND` | No | `crawl4ai` | URL extraction backend used by the agent |
| `URL_EXTRACTION_MAX_URLS_PER_MESSAGE` | No | `3` | Maximum number of URLs processed per incoming message |
| `URL_FETCH_TIMEOUT_SECONDS` | No | `25` | Per-URL extraction timeout |
| `URL_FETCH_MAX_CHARS` | No | `12000` | Maximum extracted text passed for synopsis generation |
| `URL_ALLOW_PRIVATE_NETS` | No | `false` | Allow links resolving to private/local IP ranges |
| `URL_ALLOWED_DOMAINS` | No | empty | Optional comma-separated allowlist; when set, only these domains are processed |
| `URL_BLOCKED_DOMAINS` | No | empty | Optional comma-separated blocklist for domains |
| `LINK_NOTES_FOLDER` | No | `Links` | Vault folder root where dedicated captured-link notes are written |
| `ATTACHMENTS_FOLDER` | No | `Attachments` | Vault folder root where inbound Telegram document attachments are stored |

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

If logs show `Another sync instance is already running for this vault.` repeatedly, a stale lock file is likely blocking startup.
Run:
- `docker compose down`
- `docker run --rm -v lazylogger_obsidian-config:/cfg alpine sh -lc 'find /cfg -type f \( -name "*.lock" -o -name ".lock" -o -name "lock" \) -print -delete'`
- `docker compose up -d --build`

The sync container entrypoint also cleans stale lock files at boot in recent versions.

**Agent returns "I'm having trouble thinking"**: Set `LOG_LEVEL=DEBUG` and check `docker compose logs -f agent` to see tool calls and LLM responses.

**Azure OpenAI 404**: The `AZURE_OPENAI_DEPLOYMENT` must match the exact deployment name in your Azure portal (not the model name).
