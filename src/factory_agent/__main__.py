from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "factory_agent.api.server:create_app",
        factory=True,
        host=os.getenv("FACTORY_AGENT_HOST", "127.0.0.1"),
        port=int(os.getenv("FACTORY_AGENT_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
