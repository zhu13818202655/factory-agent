-- Local/dev PostgreSQL bootstrap (ADR-0003 §7 table ownership).
--
-- factory-agent and usage-admin share ONE logical database (factory_agent):
-- each service connects with its own user and runs its own Alembic migrations
-- against it, tracked in separate version tables (alembic_version /
-- alembic_version_usage_admin). Table ownership is exclusive per service.
-- Mock MES stays on its own database.
--
-- PG16: schema "public" is owned by pg_database_owner (the database owner),
-- so usage_admin needs an explicit CREATE grant on the schema to build its own
-- tables. Default privileges make every table each service creates later
-- readable by the other service (factory-agent reads tenant_registry etc.;
-- usage-admin reads the metering tables read-only).

CREATE USER factory_agent WITH PASSWORD 'factory_agent_dev';
CREATE DATABASE factory_agent OWNER factory_agent;

CREATE USER usage_admin WITH PASSWORD 'usage_admin_dev';
GRANT CONNECT ON DATABASE factory_agent TO usage_admin;
GRANT CREATE ON SCHEMA public TO usage_admin;
ALTER DEFAULT PRIVILEGES FOR ROLE usage_admin IN SCHEMA public
    GRANT SELECT ON TABLES TO factory_agent;
ALTER DEFAULT PRIVILEGES FOR ROLE factory_agent IN SCHEMA public
    GRANT SELECT ON TABLES TO usage_admin;

CREATE USER mock_mes WITH PASSWORD 'mock_mes_dev';
CREATE DATABASE mock_mes OWNER mock_mes;
