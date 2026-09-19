"""Multi-tenant usage metering and operations statistics.

The statistics surface lives inside the factory-agent process: it reads the
metering tables this service writes, serves ``/v1/statistics/*`` behind a
platform ``Bearer`` identity, and owns the tenant master data
(``tenant_registry`` / ``admin_audit`` / ``platform_principal`` /
``usage_export``). It deliberately depends on no business-domain package so it
stays extractable into its own service if that ever becomes worthwhile.
"""
