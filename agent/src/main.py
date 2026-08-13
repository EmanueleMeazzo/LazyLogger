"""LazyLogger Agent — entry point.

Starts the Telegram bot, MCP client, LangChain agent, and health server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import structlog
from aiohttp import web
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from openai import AsyncAzureOpenAI, AsyncOpenAI

from .agent import build_agent, load_system_prompt
from .config import Settings
from .enrichment import EntityCatalog, TaxonomyCache
from .link_extractor import LinkExtractor
from .mcp_client import create_mcp_client
from .smart_search import build_smart_search_tool
from .telegram_bot import build_application

logger = structlog.get_logger()


def setup_logging(log_level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def start_health_server(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server started", port=port)
    return runner


async def async_main() -> None:
    settings = Settings()
    # utils.* resolve the timezone from os.environ, but pydantic-settings does not
    # export .env keys to the process env. Publish the validated value so local
    # `uv run` (USER_TIMEZONE only in .env) gets correct daily-note paths and
    # [HH:MM] capture-time prefixes. No-op in Docker, where compose injects it.
    os.environ["USER_TIMEZONE"] = settings.user_timezone
    setup_logging(settings.log_level)
    logger.info("Starting LazyLogger agent...")

    # Load system prompt
    system_prompt = load_system_prompt(settings.system_prompt_path)

    # Initialize MCP client and get tools
    logger.info("Connecting to MCP server...")
    mcp_client = create_mcp_client(settings)
    tools = await mcp_client.get_tools()
    # Local structured-retrieval tool, alongside the MCP tools. build_agent's
    # loop sets handle_tool_error=True on it too (it's a StructuredTool).
    tools.append(build_smart_search_tool(settings))
    logger.info("Tools loaded", tool_count=len(tools), tools=[t.name for t in tools])

    # Persistent conversation memory: a SQLite-backed checkpointer that survives
    # restarts. The connection is an async context manager owned here for the
    # whole process lifetime (closed in the finally block, alongside the MCP client).
    db_path = settings.checkpointer_db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    checkpointer_cm = AsyncSqliteSaver.from_conn_string(db_path)
    checkpointer = await checkpointer_cm.__aenter__()
    await checkpointer.setup()
    logger.info("Conversation checkpointer ready", path=db_path)

    # Create the LangChain agent
    agent = build_agent(settings, tools, system_prompt, checkpointer)

    # Build Telegram application
    telegram_app = build_application(settings)
    telegram_app.bot_data["agent"] = agent
    telegram_app.bot_data["tools"] = tools
    telegram_app.bot_data["mcp_client"] = mcp_client
    telegram_app.bot_data["link_extractor"] = LinkExtractor(settings)
    # Azure v1 API: the stock OpenAI client against the resource's /openai/v1/
    # base URL. Callers pass the deployment name as `model=` per request.
    telegram_app.bot_data["openai_client"] = AsyncOpenAI(
        api_key=settings.azure_openai_api_key,
        base_url=settings.azure_v1_base_url,
    )
    # Whisper is the exception: the v1 /audio/transcriptions route 404s on
    # Whisper deployments (it only resolves the newer transcribe models), so
    # transcription keeps the legacy deployment-scoped client. Verified against
    # the resource — swapping this to the v1 client silently breaks voice notes.
    telegram_app.bot_data["transcription_client"] = AsyncAzureOpenAI(
        api_key=settings.azure_openai_api_key,
        azure_endpoint=settings.azure_openai_endpoint,
        api_version=settings.azure_openai_api_version,
    )
    telegram_app.bot_data["taxonomy_cache"] = TaxonomyCache(
        settings.mcp_vault_path,
        settings.taxonomy_scan_limit,
        settings.taxonomy_cache_ttl_seconds,
    )
    telegram_app.bot_data["entity_catalog"] = EntityCatalog(
        settings.mcp_vault_path,
        settings.entities_folder,
        settings.entity_cache_ttl_seconds,
    )

    # Start health server
    health_runner = await start_health_server(settings.health_port)

    # Start Telegram bot (manual lifecycle for async control)
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(allowed_updates=["message"])

    logger.info("LazyLogger agent is running. Waiting for messages...")

    # Wait for shutdown signal
    stop = asyncio.Event()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        # Graceful shutdown
        logger.info("Shutting down...")
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await health_runner.cleanup()
        # Close OpenAI clients
        await telegram_app.bot_data["openai_client"].close()
        await telegram_app.bot_data["transcription_client"].close()
        # NOTE: MultiServerMCPClient (langchain-mcp-adapters >=0.1.0) is NOT an
        # async context manager — calling __aexit__ raises NotImplementedError.
        # With per-call stdio sessions there's nothing to close here; the MCP
        # subprocess is reaped when this process exits.
        # Close the persistent checkpointer connection
        await checkpointer_cm.__aexit__(None, None, None)
        logger.info("Shutdown complete.")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
