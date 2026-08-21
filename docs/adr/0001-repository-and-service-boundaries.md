# ADR-0001: Repository And Service Boundaries

- Status: Accepted
- Date: 2026-08-17
- Owners: Project maintainers

## Context

The product service, Canonical contract, Mock MES, adapters, and cross-service tests will evolve
together while the customer API is still unknown. A nested `agent-api/` directory would repeat
the repository name and make migration from the existing root Python package unnecessarily deep.
A separate Mock repository would require contract publication and coordinated versions before
there is a second consumer or a stable contract.

## Decision

- The repository root directly builds `src/factory_agent`.
- `mock-mes/` is a self-contained uv workspace child with its own package, Dockerfile, tests,
  configuration, and future migrations.
- The repository has one `.git` and one `uv.lock`.
- The packages never import each other and communicate only through versioned HTTP contracts.
- Production images and Compose topology exclude Mock MES unless its explicit development overlay
  is selected.
- Shared Python DTO packages are deferred. Canonical OpenAPI and JSON Schema are the compatibility
  boundary, and generated clients belong to their consumers.

## Consequences

- The contract, simulator, adapter, and cross-service tests can evolve together in one repository.
- Mock MES remains independently runnable and can be moved to a repository later without changing
  product imports.
- Workspace CI sees both projects, so package-boundary and contract tests are mandatory.
- A future split requires semantic contract versions and a published artifact before moving code.

## Revisit When

Reconsider a separate Mock repository when it has another real consumer, an independent team or
release cadence, a stable versioned Canonical contract, or materially slows the product CI.
