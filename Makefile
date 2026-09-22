SHELL := /bin/bash

UV ?= uv
RUN := $(UV) run --no-sync
PYTEST := $(RUN) pytest
GIT_SAFE_ENV := GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=$(CURDIR)

# --- local database helpers -------------------------------------------------
# The migration entry point reads its DSN from the environment and does NOT
# load .env itself, so every migration recipe loads it first.
VENV_PYTHON := .venv/bin/python
ENV_FILE ?= .env
PG_CONTAINER ?= factory-agent-middleware-postgres-1
PG_DB ?= factory_agent
# Usage: make migrate ACTION=downgrade REVISION=base
ACTION ?= upgrade
REVISION ?= head
LOAD_ENV := if [ -f "$(ENV_FILE)" ]; then set -a; . "./$(ENV_FILE)"; set +a; \
	else echo "warning: $(ENV_FILE) not found, using the ambient environment" >&2; fi

.DEFAULT_GOAL := help

.PHONY: help bootstrap lint typecheck test-unit test-integration
.PHONY: test-e2e security check dev pre-commit compose-config compose-up compose-down compose-reset middleware-up middleware-down middleware-reset
.PHONY: migrate migrate-status pg-grants build-images test-images

help:
	@printf '%s\n' \
		'make bootstrap         Install the complete uv project' \
		'make check             Run all repository code checks' \
		'make compose-config    Validate development Compose files' \
		'make compose-up        Start all application services' \
		'make compose-down      Stop all application services' \
		'make compose-reset     Wipe all local data volumes and restart' \
		'make middleware-up     Start local PostgreSQL and Redis' \
		'make middleware-down   Stop local PostgreSQL and Redis' \
		'make middleware-reset  Wipe local PostgreSQL and Redis data, then restart' \
		'make migrate           Run the database migrations' \
		'make migrate-status    Show the Alembic version-table head' \
		'make pg-grants         Repair schema grants on a legacy PostgreSQL volume' \
		'make build-images      Build all application images' \
		'make test-images       Run image health and non-root checks' \
		'make dev               Run factory-agent locally'

bootstrap:
	$(UV) sync --group dev

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	bash -n deploy/compose/*.sh
	bash -n scripts/*.sh

typecheck:
	$(RUN) pyright

test-unit:
	$(PYTEST) tests/unit tests/statistics/unit tests/eval

test-integration:
	$(PYTEST) tests/integration

test-e2e:
	$(PYTEST) tests/e2e

security:
	$(RUN) bandit --quiet --recursive src
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

# --- database migrations (one database, one Alembic version table; the
# statistics tables live in the same baseline; see ADR-0003 §7) ---------------

migrate: middleware-up
	@$(LOAD_ENV); $(VENV_PYTHON) -m factory_agent.persistence.migrations $(ACTION) $(REVISION)

# Three distinguishable answers, because collapsing them hides the one an
# operator actually needs: "the container is down" must not read as "nothing has
# been migrated yet". Reachability is probed separately from the version query,
# so a database that exists but has never been migrated still reports NOT
# MIGRATED rather than looking like an outage.
migrate-status:
	@if ! docker exec $(PG_CONTAINER) psql -U postgres -d $(PG_DB) \
		-tAc "SELECT 1" >/dev/null 2>&1; then \
		printf '%-30s %s\n' "alembic_version:" \
			"UNAVAILABLE: cannot reach database '$(PG_DB)' in container '$(PG_CONTAINER)'" >&2; \
		exit 1; \
	fi; \
	rev=$$(docker exec $(PG_CONTAINER) psql -U postgres -d $(PG_DB) \
		-tAc "SELECT version_num FROM alembic_version;" 2>/dev/null | tr -d '[:space:]'); \
	printf '%-30s %s\n' "alembic_version:" "$${rev:-NOT MIGRATED}"

# One-off repair for volumes initialised before init-databases.sql created the
# single application role with ownership of the schema.
pg-grants:
	docker exec $(PG_CONTAINER) psql -U postgres -d factory_agent \
		-c "ALTER SCHEMA public OWNER TO factory_agent;" \
		-c "GRANT ALL ON ALL TABLES IN SCHEMA public TO factory_agent;" \
		-c "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO factory_agent;"

build-images:
	docker build --tag factory-agent:dev --file Dockerfile .

test-images:
	bash scripts/verify_images.sh

dev:
	$(RUN) factory-agent
