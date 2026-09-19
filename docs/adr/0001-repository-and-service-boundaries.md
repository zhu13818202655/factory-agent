# ADR-0001: Repository And Service Boundaries

- Status: Accepted
- Date: 2026-08-17
- Owners: Project maintainers

## Context

One deployment serves users from many company/factory tenants. MES business interactions use an
active tenant-local `DataScope`; platform usage aggregation uses a separate `PlatformScope`. The
repository builds one application that serves both surfaces, and the boundary between them must
stay enforceable in code.

## Decision

- The repository root builds `src/factory_agent`, one installable package.
- The platform statistics surface lives inside that package at `src/factory_agent/statistics/` and
  is mounted at `/v1/statistics`. It shares the application PostgreSQL, reads the tables it does
  not own read-only, and never calls MES endpoints.
- The repository has one `.git` and one `uv.lock`.
- The statistics surface never depends on the business domain, and the business domain never
  imports it; the single business-side entry point to its tables is the
  `ports/tenant_registry.py` reader. There is no cross-service usage transport and no usage-event
  contract: factory-agent writes its metering tables directly into PostgreSQL in a separate
  transaction after its business commit, and the statistics surface only reads them (table
  ownership: ADR-0003 §7). The shared-table read boundary for business code is `tenant_registry`
  (ADR-0003 §4.3).
- Production deployment runs one application process, which serves both the business routes and
  the statistics routes. Changing statistics code therefore requires restarting the process that
  answers questions (ADR-0003 §14).
- There is no OpenAPI/JSON Schema contract directory in the repository. The customer MES interface
  contract lives in `docs/product/AI问答对外接口-整理.md`, the reviewed operation catalog is
  `configs/knowledge/apis.yaml`, and the product requirements live in
  `docs/product/需求及方案整理.md`. Shared DTO packages are not introduced; generated clients belong
  to their consumers.

## Consequences

- The product and the statistics surface, adapters, and their tests evolve together in one
  repository.
- The statistics surface can still be split out later — into its own service or repository —
  without changing the MES execution boundary, because the dependency direction is enforced in
  code.
- CI treats the repository as a single application. Package-boundary tests (no dependency from
  `statistics` on the business domain, and none in the other direction), health-surface tests, and
  produced-usage-event hygiene tests are mandatory.
- A future repository split requires stable published contracts and an independent release cadence
  before moving code.

## Revisit When

Split the statistics surface into a separate service or repository when it has an independent team
or release cadence, stable published contracts, materially slows shared CI, or when measured
statistics concurrency or database load starts affecting question-answering latency
(ADR-0003 §15). Revisit the shared-database direct write when measured throughput, multiple
consumers, replay, or cross-region delivery justify Kafka/Redpanda or an analytical replica.
