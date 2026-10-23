-- +goose Up
-- These are the MCP edge's owned Connection and OAuth records, independent of Actions lifecycle state.
CREATE TABLE external_connection (
    id UUID PRIMARY KEY,
    display_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    bound_caller JSONB,
    binding_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE external_connection_grant (
    id UUID PRIMARY KEY,
    connection_id UUID NOT NULL REFERENCES external_connection (id),
    revision INTEGER NOT NULL,
    issuer TEXT NOT NULL,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    activated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    original_caller JSONB NOT NULL,
    UNIQUE (connection_id, revision)
);

CREATE TABLE connection_enrollment (
    id UUID PRIMARY KEY,
    handle_hash TEXT NOT NULL UNIQUE,
    issuer TEXT NOT NULL,
    client_id TEXT NOT NULL,
    client_name TEXT,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    upstream_url TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    version INTEGER NOT NULL,
    operator_issuer TEXT,
    operator_subject TEXT,
    verdict TEXT,
    display_name TEXT,
    exchange_claimed_at TIMESTAMPTZ,
    connection_id UUID REFERENCES external_connection (id),
    connection_version INTEGER,
    caller JSONB,
    UNIQUE (client_id, redirect_uri, code_challenge)
);

CREATE TABLE oauth_registered_client (
    client_id TEXT PRIMARY KEY,
    metadata JSONB NOT NULL
);

CREATE TABLE oauth_authorization (
    id UUID PRIMARY KEY REFERENCES connection_enrollment (id),
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    state TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    upstream_state_hash TEXT NOT NULL UNIQUE,
    upstream_verifier TEXT NOT NULL,
    code_hash TEXT UNIQUE,
    signed_in_at TIMESTAMPTZ,
    upstream_expires_at TIMESTAMPTZ,
    upstream_refresh_token TEXT,
    exchanged_at TIMESTAMPTZ,
    CONSTRAINT oauth_authorization_signed_in_whole CHECK (
        (code_hash IS NULL) = (signed_in_at IS NULL) AND (code_hash IS NULL) = (upstream_expires_at IS NULL)
    ),
    CONSTRAINT oauth_authorization_exchanged_after_sign_in CHECK (exchanged_at IS NULL OR code_hash IS NOT NULL)
);

CREATE TABLE oauth_token (
    token_hash TEXT PRIMARY KEY,
    authorization_id UUID NOT NULL REFERENCES oauth_authorization (id),
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    expires_at TIMESTAMPTZ NOT NULL,
    rotated_at TIMESTAMPTZ,
    CONSTRAINT oauth_token_rotated_refresh CHECK (kind = 'refresh' OR rotated_at IS NULL)
);
