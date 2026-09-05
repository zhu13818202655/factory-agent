SHELL := /bin/bash

UV ?= uv
RUN := $(UV) run --no-sync
PYTEST := $(RUN) pytest
GIT_SAFE_ENV := GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=$(CURDIR)

# --- local database helpers -------------------------------------------------
# Both migration entry points read their DSN from the environment and do NOT
# load .env themselves, so every migration recipe loads it first.
VENV_PYTHON := .venv/bin/python
ENV_FILE ?= .env
PG_CONTAINER ?= factory-agent-middleware-postgres-1
# Usage: make migrate-agent ACTION=downgrade REVISION=-1
ACTION ?= upgrade
REVISION ?= head
LOAD_ENV := if [ -f "$(ENV_FILE)" ]; then set -a; . "./$(ENV_FILE)"; set +a; \
	else echo "warning: $(ENV_FILE) not found, using the ambient environment" >&2; fi

.DEFAULT_GOAL := help

.PHONY: help bootstrap lint typecheck test-unit test-integration
.PHONY: test-e2e security check dev dev-mock dev-usage-admin pre-commit compose-config compose-up compose-down compose-reset middleware-up middleware-down middleware-reset
.PHONY: migrate migrate-agent migrate-usage-admin migrate-status pg-grants build-images test-images

help:
	@printf '%s\n' \
		'make bootstrap         Install the complete uv workspace' \
		'make check             Run all repository code checks' \
		'make compose-config    Validate development Compose files' \
		'make compose-up        Start all application services' \
		'make compose-down      Stop all application services' \
		'make compose-reset     Wipe all local data volumes and restart' \
		'make middleware-up     Start local PostgreSQL and Redis' \
		'make middleware-down   Stop local PostgreSQL and Redis' \
		'make middleware-reset  Wipe local PostgreSQL and Redis data, then restart' \
		'make migrate           Run factory-agent then usage-admin migrations' \
		'make migrate-agent     Migrate only the factory-agent schema' \
		'make migrate-usage-admin  Migrate only the usage-admin schema' \
		'make migrate-status    Show both Alembic version-table heads' \
		'make pg-grants         Repair schema grants on a legacy PostgreSQL volume' \
		'make build-images      Build all application images' \
		'make test-images       Run image health and non-root checks' \
		'make dev               Run factory-agent locally' \
		'make dev-mock          Run mock-mes locally' \
		'make dev-usage-admin   Run usage-admin locally'

bootstrap:
	$(UV) sync --all-packages --group dev

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	bash -n deploy/compose/*.sh
	bash -n scripts/*.sh

typecheck:
	$(RUN) pyright

test-unit:
	$(PYTEST) tests/unit tests/eval mock-mes/tests/unit usage-admin/tests/unit

test-integration:
	$(PYTEST) tests/integration usage-admin/tests/integration

test-e2e:
	$(PYTEST) tests/e2e

security:
	$(RUN) bandit --quiet --recursive src mock-mes/src usage-admin/src
	$(RUN) pip-audit --skip-editable
	$(PYTEST) tests/security

check: lint typecheck test-unit test-integration test-e2e security

pre-commit:
	$(GIT_SAFE_ENV) $(RUN) pre-commit run --all-files
	$(GIT_SAFE_ENV) git ls-files --others --exclude-standard -z | \
		xargs -0 -r env $(GIT_SAFE_ENV) $(RUN) pre-commit run --files

compose-config:
	bash deploy/compose/check.sh all
	bash deploy/compose/check.sh middleware

compose-up:
	bash deploy/compose/start.sh all

compose-down:
	bash deploy/compose/stop.sh all

# Destructive: deletes the named volumes, so every database and the Redis AOF
# are gone. Pass CONFIRM=1 to skip the interactive prompt.
compose-reset:
	bash deploy/compose/reset.sh all $(if $(filter 1,$(CONFIRM)),--yes,)

middleware-up:
	bash deploy/compose/start.sh middleware

middleware-down:
	bash deploy/compose/stop.sh middleware

# Destructive: deletes the named volumes, so every database and the Redis AOF
# are gone. Pass CONFIRM=1 to skip the interactive prompt.
middleware-reset:
	bash deploy/compose/reset.sh middleware $(if $(filter 1,$(CONFIRM)),--yes,)

# --- database migrations (factory-agent and usage-admin share one database,
# each with its own Alembic version table; see ADR-0003 §7) -------------------

migrate: migrate-agent migrate-usage-admin

migrate-agent: middleware-up
	@printf '== factory-agent schema ==\n'
	@$(LOAD_ENV); $(VENV_PYTHON) -m factory_agent.persistence.migrations $(ACTION) $(REVISION)

migrate-usage-admin: middleware-up
	@printf '== usage-admin schema ==\n'
	@$(LOAD_ENV); $(VENV_PYTHON) -m usage_admin.migrations $(ACTION) $(REVISION) || { \
		printf '\n若报 permission denied for schema public：先跑 make pg-grants 补授权再重试\n' >&2; \
		exit 1; \
	}

migrate-status:
	@for table in alembic_version alembic_version_usage_admin; do \
		rev=$$(docker exec $(PG_CONTAINER) psql -U postgres -d factory_agent \
			-tAc "SELECT version_num FROM $$table;" 2>/dev/null | tr -d '[:space:]'); \
		printf '%-30s %s\n' "$$table:" "$${rev:-NOT MIGRATED}"; \
	done

# One-off repair for volumes initialised before init-databases.sql granted
# usage_admin CREATE on schema public; also re-applies the cross-service
# SELECT grants after new tables are created.
pg-grants:
	docker exec $(PG_CONTAINER) psql -U postgres -d factory_agent \
		-c "GRANT CREATE ON SCHEMA public TO usage_admin;" \
		-c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO usage_admin;" \
		-c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO factory_agent;"

build-images:
	docker build --tag factory-agent:dev --file Dockerfile .
	docker build --tag mock-mes:dev --file mock-mes/Dockerfile .
	docker build --tag usage-admin:dev --file usage-admin/Dockerfile .

test-images:
	bash scripts/verify_images.sh

dev:
	$(RUN) factory-agent

dev-mock:
	$(RUN) mock-mes

dev-usage-admin:
	$(RUN) usage-admin
