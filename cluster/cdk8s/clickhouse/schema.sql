-- Database creation is a ClickHouse-admin-only operation (not grantable to an
-- app identity scoped to its own database), so it stays here. Everything
-- inside each database is owned by its app: aiquota applies aiquota/schema.sql
-- at startup; Langfuse's own external migration runner owns everything inside
-- langfuse.
CREATE DATABASE IF NOT EXISTS langfuse ON CLUSTER default;

CREATE DATABASE IF NOT EXISTS aiquota ON CLUSTER default;
