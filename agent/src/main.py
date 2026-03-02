"""LazyLogger Agent — entry point.

Starts the Telegram bot, MCP client, LangChain agent, and health server.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
from aiohttp import web

from .agent import build_agent, load_system_prompt
from .config import Settings
from .mcp_client import create_mcp_client
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
    setup_logging(settings.log_level)
    logger.info("Starting LazyLogger agent...")

    # Load system prompt
    system_prompt = load_system_prompt(settings.system_prompt_path)

    # Initialize MCP client and get tools
    logger.info("Connecting to MCP server...")
    mcp_client = create_mcp_client(settings)
    tools = await mcp_client.get_tools()
    logger.info("MCP tools loaded", tool_count=len(tools), tools=[t.name for t in tools])

    # Create the LangChain agent
    agent = build_agent(settings, tools, system_prompt)

    # Build Telegram application
    telegram_app = build_application(settings)
    telegram_app.bot_data["agent"] = agent
    telegram_app.bot_data["tools"] = tools
    telegram_app.bot_data["mcp_client"] = mcp_client

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
        # Close MCP client (stops subprocess)
        if hasattr(mcp_client, "__aexit__"):
            await mcp_client.__aexit__(None, None, None)
        logger.info("Shutdown complete.")


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
