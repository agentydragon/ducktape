-- +goose Up
-- The retired Python Actions OAuth proxy no longer reads these fields or its private KV table. The Go
-- authorization server and connection API continue to use the other OAuth tables and activation_deadline.
ALTER TABLE connection_enrollment
    DROP COLUMN browser_hash,
    DROP COLUMN decision_digest;

ALTER TABLE external_connection_grant
    DROP COLUMN request_digest;

-- This table is absent from fresh Goose databases, but contains retired FastMCP state in upgraded databases.
DROP TABLE IF EXISTS agentplane_oauth_kv;
