-- Object-level GRANTs for the CNPG-managed `plaid_ro` role.
-- CNPG owns role creation and password sync through Cluster.spec.managed.roles;
-- this script grants database/schema/object privileges that CNPG does not manage.
GRANT CONNECT ON DATABASE plaidmcp TO plaid_ro;
GRANT USAGE ON SCHEMA public TO plaid_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO plaid_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO plaid_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO plaid_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO plaid_ro;

-- plaid-spend can read only the source tables used to compute its shared view.
GRANT CONNECT ON DATABASE plaidmcp TO plaid_spend;
GRANT USAGE ON SCHEMA public TO plaid_spend;
GRANT SELECT ON TABLE links, accounts, transactions, liability_credit_snapshots TO plaid_spend;
