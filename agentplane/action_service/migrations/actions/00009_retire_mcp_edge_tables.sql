-- +goose Up
-- The standalone MCP edge owns these Connection and external OAuth records in its
-- separate database. The operator approved deletion of the eight legacy Actions
-- records on 2026-11-01. Do not drop Actions' upstream mcp_oauth_token_state or
-- mcp_server_linkage: those remain live here.
-- No CASCADE: an unexpected outside dependency must fail the migration.
DROP TABLE oauth_token;
DROP TABLE oauth_authorization;
DROP TABLE oauth_registered_client;
DROP TABLE connection_enrollment;
DROP TABLE external_connection_grant;
DROP TABLE external_connection;
