from __future__ import annotations

import uvicorn

from usage_admin.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "usage_admin.api.server:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
