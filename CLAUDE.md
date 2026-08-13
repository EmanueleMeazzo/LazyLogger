# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Big Picture

LazyLogger is a self-hosted AI agent that bridges a Telegram bot to an Obsidian vault. Two Docker services share a vault volume:

- **`obsidian-sync`** (Node 22): runs `obsidian-headless` (`ob sync --continuous --path /vault`) to keep the vault in `vault-data` Docker volume in sync with Obsidian's cloud.
- **`agent`** (Python 3.13 + Node 22): runs the Python app, which spawns the Obsidian MCP server (`@mauricio.wolff/mcp-obsidian`) as a **stdio subprocess** inside the same container. The agent talks to Telegram (long polling), reads/writes the vault via MCP tools, and exposes a health endpoint on `:8080`.

The agent does **not** run MCP over the network — `mcp_client.py` uses `MultiServerMCPClient` with `transport: "stdio"`, so the MCP server lifetime is tied to the agent process.

## Request Flow (read this before changing handlers)

`agent/src/telegram_bot.py::handle_message` is the single entry for non-command messages. It branches by attachment type **before** going to the LLM:

1. **Photo** → save bytes to `Attachments/YYYY/MM/`, call Azure OpenAI multimodal (`_analyze_photo_with_azure`) for a factual extract, then prompt the agent to log both the image link and the summary into today's daily note.
2. **Non-audio document** → save to `Attachments/YYYY/MM/`, prompt the agent to append a markdown link in today's `## Attachments` section. The Python code writes the file directly; the agent only edits the daily note.
3. **Voice / audio / audio-document** → transcribe via Azure Whisper (`AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT`), prefix with `[Transcribed audio] `, then fall through to text routing.
4. **Text** (`_process_user_text`):
   - If it contains URLs and `URL_EXTRACTION_ENABLED`, fetch via Crawl4AI in parallel and route each through `_build_link_capture_prompt` → one dedicated link note per URL plus a backlink in today's note. Other content in the message is dropped on this path.
   - Else if `_is_direct_request` returns False (no `?` and first word not in `REQUEST_PREFIXES`), route through `_build_memory_capture_prompt` to append as a memory in today's daily note.
   - Else send raw text to the agent.

Daily-note path is `YYYY/MM/YYYYMMDD.md` from `utils.today_daily_note_path()`, which respects `USER_TIMEZONE`.

## Agent Internals

- `agent.build_agent` uses `langchain.agents.create_agent` (the modern entry point — **not** `create_react_agent`) with a `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` created in `main.async_main` (`CHECKPOINTER_DB_PATH`, on the `agent-data` volume — history survives restarts).
- Conversation memory is **session-scoped**, not chat-scoped: `telegram_bot._resolve_thread_id` returns `{chat_id}:{YYYYMMDD-HHMMSS}` and rolls the stamp when the chat has been idle for `CONVERSATION_IDLE_MINUTES` or the local date changes. Session state lives in `context.chat_data`. A bare `str(chat_id)` thread grows without bound and eventually 400s on context length — don't go back to it.
- `agent.build_trim_middleware` is the backstop: a `@before_model` middleware that runs `trim_messages` at `CONVERSATION_MAX_TOKENS`. Keep `start_on="human"` — trimming mid `tool_calls`/`tool` pair orphans the call and the API rejects the request.
- All MCP tools get `tool.handle_tool_error = True` so the LLM sees errors and can recover instead of crashing the run.
- The LLM is `ChatOpenAI` from `langchain-openai` pointed at Azure's **v1 API** (`Settings.azure_v1_base_url` = endpoint + `/openai/v1/`); the deployment name goes in `model=`. Do **not** add a `temperature` parameter — modern Azure deployments (gpt-5, etc.) reject it.
- There are **two** OpenAI clients in `bot_data`, and they are not interchangeable. `openai_client` is `AsyncOpenAI` on the v1 base URL (vision + enrichment). `transcription_client` is `AsyncAzureOpenAI` on the legacy deployment-scoped route with `AZURE_OPENAI_API_VERSION` — Azure's v1 `/audio/transcriptions` returns `DeploymentNotFound` for Whisper deployments, so pointing transcription at `openai_client` silently breaks voice notes.
- `main.async_main` uses **manual** `python-telegram-bot` lifecycle (`initialize` → `start` → `updater.start_polling` → wait → reverse). Do not switch to `Application.run_polling()` — it owns the loop and will fight the health server / MCP shutdown.
- System prompt lives in `agent/system_prompt.md` (mounted at `/app/system_prompt.md`) and is loaded once at startup. Edit the file rather than hardcoding strings in Python.

## Auth and Safety

- Authorization is by **Telegram username** (not user ID). `Settings.get_authorized_users()` lowercases and strips `@`; `_check_authorized` compares case-insensitively. Users without a `username` set on their Telegram account cannot be authorized.
- `LinkExtractor._is_allowed_url` blocks non-http(s), enforces `URL_ALLOWED_DOMAINS`/`URL_BLOCKED_DOMAINS`, and (unless `URL_ALLOW_PRIVATE_NETS=true`) does a DNS resolution and rejects private/loopback/link-local/reserved/multicast IPs. Don't bypass this — it's the SSRF guard for the URL capture path.
- The agent system prompt forbids deleting or overwriting whole notes; respect this when adding new tool flows.

## Commands

All `docker compose` commands run from the repo root.

```bash
# Build + run everything
docker compose up -d --build

# First-time Obsidian Sync setup (interactive, do once per VPS)
docker compose run --rm obsidian-sync ob login
docker compose run --rm obsidian-sync ob sync-list-remote
docker compose run --rm obsidian-sync ob sync-setup --vault "Vault Name" --path /vault

# Logs
docker compose logs -f agent
docker compose logs -f obsidian-sync

# Health probe (inside the agent container, this is the actual healthcheck)
curl http://localhost:8080/health   # only reachable from inside the container

# Inspect health state if compose is stuck on `waiting`
docker inspect --format='{{json .State.Health}}' obsidian-sync
```

### Python development (inside `agent/`)

The agent uses **uv**. The repo also has an unrelated top-level `.venv/` — ignore it; the source of truth is `agent/pyproject.toml` + `agent/uv.lock`.

```bash
cd agent
uv sync                                              # install deps
uv run pytest                                        # run all tests
uv run pytest tests/test_config.py                   # one file
uv run pytest tests/test_config.py::TestSettings::test_defaults   # one test
uv run python -m src.main                            # run the agent (needs .env at repo root or in CWD)
```

Tests under `agent/tests/` import from `src.*`; run pytest from the `agent/` directory so the import path resolves.

There is no linter or formatter configured — match existing style (`from __future__ import annotations`, structlog logging, dataclasses for payloads, Pydantic for settings).

## Stale-Lock Recovery

If `obsidian-sync` logs `Another sync instance is already running for this vault.` on every restart, a stale lock file is jammed in the `obsidian-config` volume. The entrypoint cleans these at boot, but the manual fix is:

```bash
docker compose down
docker run --rm -v lazylogger_obsidian-config:/cfg alpine \
  sh -lc 'find /cfg \( -name "*.lock" -o -name ".lock" -o -name "lock" \) -print -exec rm -rf {} +'
docker compose up -d --build
```

## Conventions

- Vault writes that aren't note edits (binary attachments) happen in Python and live under `Attachments/YYYY/MM/` (`ATTACHMENTS_FOLDER`); the agent only edits the markdown that links to them.
- Captured-link notes go under `Links/YYYY/MM/{date}-{slug}-{hash}.md` (`LINK_NOTES_FOLDER`); the path is built by `LinkExtractor._build_note_path` and passed to the agent — don't have the LLM invent paths.
- `TASKS.md` is a frozen build log from initial bring-up and is gitignored on a per-clone basis. Don't update it for ongoing work.
- `LazyLogger-specs.md` is the original design spec; treat it as historical context, not as current behavior.
