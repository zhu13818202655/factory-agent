from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "mock_mes.api.server:create_app",
        factory=True,
        host=os.getenv("MOCK_MES_HOST", "127.0.0.1"),
        port=int(os.getenv("MOCK_MES_PORT", "8010")),
    )


if __name__ == "__main__":
    main()
