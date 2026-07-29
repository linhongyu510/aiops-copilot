"""Cross-platform Uvicorn entry point.

Psycopg's asynchronous implementation requires the selector event loop on
Windows, so the policy must be installed before importing the application.
"""

from __future__ import annotations

import argparse
import asyncio
import os


def configure_event_loop() -> None:
    """Use the event loop supported by psycopg on Windows."""
    if os.name == "nt" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Return Uvicorn's Windows-compatible loop factory."""
    return asyncio.SelectorEventLoop()


def main() -> None:
    configure_event_loop()

    import uvicorn

    from app.config import config

    parser = argparse.ArgumentParser(description="Run AIOps Copilot API")
    parser.add_argument("--host", default=config.host)
    parser.add_argument("--port", type=int, default=config.port)
    parser.add_argument("--reload", action="store_true", default=config.debug)
    args = parser.parse_args()
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        loop="app.run:selector_loop_factory" if os.name == "nt" else "auto",
    )


if __name__ == "__main__":
    main()
