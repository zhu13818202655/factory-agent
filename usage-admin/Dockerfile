ARG TARGETARCH=amd64

FROM --platform=linux/${TARGETARCH} python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

RUN pip install --no-cache-dir uv==0.9.15

COPY pyproject.toml uv.lock README.md ./
# uv resolves the whole workspace lock; copy member metadata only, not their source.
COPY usage-admin/pyproject.toml usage-admin/README.md ./usage-admin/
COPY usage-admin/src ./usage-admin/src

RUN uv sync --frozen --no-dev --package usage-admin --no-editable

FROM --platform=linux/${TARGETARCH} python:3.12-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 app

COPY --from=builder /app/.venv /app/.venv
# Alembic assets live at /app root so `python -m usage_admin.migrations` can
# find them by walking up from site-packages (auto-migration entrypoint).
COPY usage-admin/alembic.ini ./alembic.ini
COPY usage-admin/migrations ./migrations

USER app

EXPOSE 8020

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8020/health/live', timeout=2)"]

CMD ["uvicorn", "usage_admin.api.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8020"]
