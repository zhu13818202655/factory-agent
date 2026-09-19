-- Local/dev PostgreSQL bootstrap (ADR-0003 §7 table ownership).
--
-- One logical database (factory_agent), one application user, one Alembic
-- version table. The business tables, the metering tables, and the platform
-- surface's own tables (tenant_registry / admin_audit / platform_principal /
-- usage_export) all live here under the single baseline migration, so no
-- cross-user grants are needed.
--
-- PG16: schema "public" is owned by pg_database_owner (the database owner), so
-- making factory_agent the database owner is what grants it CREATE on the
-- schema.

CREATE USER factory_agent WITH PASSWORD 'factory_agent_dev';
CREATE DATABASE factory_agent OWNER factory_agent;

\connect factory_agent
