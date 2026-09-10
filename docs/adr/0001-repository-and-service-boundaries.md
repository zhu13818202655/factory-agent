# ADR-0001: Repository And Service Boundaries

- Status: Accepted
- Date: 2026-08-17
- Owners: Project maintainers

## Context

One deployment serves users from many company/factory tenants. MES business interactions use an
active tenant-local `DataScope`; platform usage aggregation uses a separate `PlatformScope`. The
repository co-hosts two buildable units whose boundaries must stay enforceable: the product
service and the usage administration service.

## Decision

- The repository root directly builds `src/factory_agent`.
- `usage-admin/` is a self-contained production uv workspace child with its own package, Dockerfile,
  tests, configuration, and migrations. It shares the application PostgreSQL, reads the tables it
  does not own read-only, and never calls MES endpoints.
- The repository has one `.git` and one `uv.lock`.
- The packages never import each other. There is no cross-service usage transport and no
  usage-event contract: factory-agent writes its metering tables directly into the shared
  PostgreSQL in a separate transaction after its business commit, and usage-admin only reads them
  (table ownership: ADR-0003 §7). The single shared-table read boundary for factory-agent is
  `tenant_registry` (ADR-0003 §4.3).
- Production usage metering deploys `usage-admin` independently from `factory-agent`.
- There is no OpenAPI/JSON Schema contract directory in the repository. The customer MES interface
  contract lives in `docs/product/AI问答对外接口-整理.md`, the reviewed operation catalog is
  `configs/knowledge/apis.yaml`, and the product requirements live in
  `docs/product/需求及方案整理.md`. Shared DTO packages are not introduced; generated clients belong
  to their consumers.

## Consequences

- The product, usage administration service, adapters, and cross-service tests evolve
  together in one repository.
- Usage administration can scale, adopt Kafka or an analytical replica, and move to a repository
  later without changing the MES execution boundary.
- Workspace CI treats both packages as separate applications. Package-boundary tests (no
  cross-import), health-surface tests, and produced-usage-event hygiene tests are mandatory.
- A future repository split requires stable published contracts and an independent release cadence
  before moving code.

## Revisit When

Split a service into a separate repository when it has an independent team or release cadence,
stable published contracts, or materially slows shared CI. Revisit the shared-database direct write
when measured throughput, multiple consumers, replay, or cross-region delivery justify
Kafka/Redpanda or an analytical replica (ADR-0003 §15).
