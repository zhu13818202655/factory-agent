import argparse
import asyncio
import sys

import uvicorn

from factory_agent.config import get_settings


def create_selector_event_loop() -> asyncio.AbstractEventLoop:
    """Psycopg async connections require a selector event loop on Windows."""
    return asyncio.SelectorEventLoop()


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run the factory-agent API.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    uvicorn.run(
        "factory_agent.api.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        loop=(
            "factory_agent.__main__:create_selector_event_loop"
            if sys.platform == "win32"
            else "auto"
        ),
    )


if __name__ == "__main__":
    main()
