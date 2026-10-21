-- +goose Up
-- The Python service bound a grant pending, compared a retried binding against request_digest, and activated the
-- grant before activation_deadline once FastMCP had issued its tokens. The Go authorization server creates a grant
-- active in the transaction that issues its tokens, which is never retried, so it writes neither column. Both are
-- dropped once the Python service no longer writes them.
ALTER TABLE external_connection_grant ALTER COLUMN request_digest DROP NOT NULL;
ALTER TABLE external_connection_grant ALTER COLUMN activation_deadline DROP NOT NULL;
