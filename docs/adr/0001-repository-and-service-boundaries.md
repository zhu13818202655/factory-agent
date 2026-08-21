# ADR-0001: Repository And Service Boundaries

- Status: Accepted
- Date: 2026-08-17
- Owners: Project maintainers

## Context

The product service, Canonical contract, Mock MES, usage administration service, adapters, and
cross-service tests evolve together while customer APIs and production volumes are still unknown.
One deployment serves users from many company/factory tenants. MES business interactions use an
active tenant-local `DataScope`; platform usage aggregation uses a separate `PlatformScope`.

## Decision

- The repository root directly builds `src/factory_agent`.
- `mock-mes/` is a self-contained uv workspace child with its own package, Dockerfile, tests,
  configuration, and future migrations.
- `usage-admin/` is a self-contained production uv workspace child with its own package, Dockerfile,
  tests, configuration, migrations, and database. It consumes versioned, redacted usage events and
  never calls MES endpoints.
- The repository has one `.git` and one `uv.lock`.
- The packages never import each other and communicate only through versioned HTTP and event contracts.
- Production images and Compose topology exclude Mock MES unless its explicit development overlay
  is selected. Production usage metering deploys `usage-admin` independently from `factory-agent`.
- Shared Python DTO packages are deferred. Canonical OpenAPI and JSON Schema are the compatibility
  boundary, and generated clients belong to their consumers.

## Consequences

- The product, contract, simulator, usage administration service, adapters, and cross-service tests
  can evolve together in one repository.
- Mock MES remains independently runnable and can be moved to a repository later without changing
  product imports.
- Usage administration can scale, adopt Kafka or an analytical replica, and move to a repository
  later without changing the MES execution boundary.
- Workspace CI treats all three packages as separate applications; package-boundary, event-contract,
  and HTTP-contract tests are mandatory.
- A future split requires semantic contract versions and a published artifact before moving code.

## Revisit When

Split a service into a separate repository when it has an independent team or release cadence,
stable published contracts, or materially slows shared CI. Revisit the usage-event transport when
measured throughput, multiple consumers, replay, or cross-region delivery justify Kafka/Redpanda.
