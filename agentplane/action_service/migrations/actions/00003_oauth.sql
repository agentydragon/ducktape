-- +goose Up
-- The OAuth authorization server's own state (agentplane/mcp_edge/authserver). Codes and tokens are stored only as
-- the SHA-256 hex digest of their value.

-- A client registered by Dynamic Client Registration, with the metadata its registration answered.
CREATE TABLE oauth_registered_client (
    client_id TEXT PRIMARY KEY,
    metadata JSONB NOT NULL
);

-- An authorization request and the code its upstream sign-in earns. It asks consent through the enrollment of the
-- same id, whose grant the code exchange creates under that id too.
CREATE TABLE oauth_authorization (
    id UUID PRIMARY KEY REFERENCES connection_enrollment (id),
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    -- The client's state, returned with the code; NULL when it sent none.
    state TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    -- The upstream leg's state and PKCE verifier.
    upstream_state_hash TEXT NOT NULL UNIQUE,
    upstream_verifier TEXT NOT NULL,
    -- Set by the upstream sign-in of the pinned operator: the code, and that operator's upstream session, which a
    -- refresh renews.
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

-- An access or refresh token of the authorization's family.
CREATE TABLE oauth_token (
    token_hash TEXT PRIMARY KEY,
    authorization_id UUID NOT NULL REFERENCES oauth_authorization (id),
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    expires_at TIMESTAMPTZ NOT NULL,
    -- When a refresh spent the refresh token; NULL while it is unspent, and always for an access token.
    rotated_at TIMESTAMPTZ,
    CONSTRAINT oauth_token_rotated_refresh CHECK (kind = 'refresh' OR rotated_at IS NULL)
);
