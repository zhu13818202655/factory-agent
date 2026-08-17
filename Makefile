SHELL := /bin/bash

UV ?= uv
RUN := $(UV) run --no-sync
PYTEST := $(RUN) pytest
GIT_SAFE_ENV := GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=$(CURDIR)

.DEFAULT_GOAL := help

.PHONY: help bootstrap policy lint typecheck test-unit test-contract test-integration
.PHONY: test-e2e security check dev dev-mock pre-commit compose-config build-images test-images

help:
	@printf '%s\n' \
		'make bootstrap         Install the complete uv workspace' \
		'make check             Run all Phase 0 blocking gates' \
		'make compose-config    Validate development Compose files' \
		'make build-images      Build both application images' \
		'make test-images       Run image health and non-root checks' \
		'make dev               Run factory-agent locally' \
		'make dev-mock          Run mock-mes locally'

bootstrap:
	$(UV) sync --all-packages --group dev

policy:
	$(RUN) check-jsonschema \
		--schemafile workitems/schema/story.schema.json \
		workitems/stories/*.yaml

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	bash -n scripts/*.sh

typecheck:
	$(RUN) pyright

test-unit:
	$(PYTEST) tests/unit mock-mes/tests/unit

test-contract:
	$(PYTEST) tests/contract

test-integration:
	$(PYTEST) tests/integration

test-e2e:
	$(PYTEST) tests/e2e

security:
	$(RUN) bandit --quiet --recursive src mock-mes/src
	$(RUN) pip-audit --skip-editable
	$(PYTEST) tests/security

check: policy lint typecheck test-unit test-contract test-integration test-e2e security

pre-commit:
	$(GIT_SAFE_ENV) $(RUN) pre-commit run --all-files

compose-config:
	docker compose \
		-f deploy/compose/compose.yml \
		-f deploy/compose/compose.mock.yml \
		config --quiet

build-images:
	docker build --tag factory-agent:dev --file Dockerfile .
	docker build --tag mock-mes:dev --file mock-mes/Dockerfile .

test-images:
	bash scripts/verify_images.sh

dev:
	$(RUN) factory-agent

dev-mock:
	$(RUN) mock-mes
