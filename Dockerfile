ARG TARGETARCH=amd64

FROM --platform=linux/${TARGETARCH} python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

RUN pip install --no-cache-dir uv==0.9.15

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable

FROM --platform=linux/${TARGETARCH} python:3.12-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 app

# The artifact export store and the statistics report store live here when
# mounted. Pre-creating them as app-owned makes a named volume inherit the
# right owner on first mount; without it docker creates the volume root-owned
# and uid 10001 could never write (exports would silently degrade to
# "unavailable").
RUN mkdir -p /app/data/exports /app/data/statistics-exports && chown -R app:app /app/data

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
# Reviewed knowledge: catalog/metrics/L1 recipes are read at startup (load_catalog,
# model registry, recipe registry) and must be present in the runtime image.
COPY configs ./configs

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]

CMD ["uvicorn", "factory_agent.api.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
