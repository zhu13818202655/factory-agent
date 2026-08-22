from __future__ import annotations

import uvicorn

from factory_agent.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "factory_agent.api.server:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
