// Package oauthspike is a spike, not production code: the Action Service's OAuth authorization server on
// zitadel/oidc op, serving authorize and token (authorization code with S256 PKCE, and refresh) to a client
// identified by a Client ID Metadata Document, over tables of its own. Its tests drive the flow with the go-sdk MCP
// client and record what op does and does not do for a plain OAuth 2.1 client.
package oauthspike

import (
	"context"
	"database/sql"
)

// Migrate creates the spike's tables. The real package would ship them as goose migrations.
func Migrate(ctx context.Context, db *sql.DB) error {
	_, err := db.ExecContext(ctx, schema)
	return err
}

// One authorization row is both op's auth request and the durable enrollment the consent page decides: it exists
// before consent, holds the one code, and is claimed once by the code exchange. A refresh family is the token chain
// one claimed authorization starts; the real package would name the Connections grant on it instead of copying the
// ServiceAccount.
const schema = `
CREATE TABLE oauth_client (
	client_id text PRIMARY KEY,
	document jsonb NOT NULL,
	fetched_at timestamptz NOT NULL
);

CREATE TABLE oauth_authorization (
	id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	client_id text NOT NULL,
	redirect_uri text NOT NULL,
	scopes text[] NOT NULL,
	state text NOT NULL,
	code_challenge text NOT NULL,
	resource text NOT NULL,
	consent_handle_hash bytea NOT NULL UNIQUE,
	expires_at timestamptz NOT NULL,
	service_account text,
	decided_at timestamptz,
	code_hash bytea UNIQUE,
	code_expires_at timestamptz,
	exchanged_at timestamptz,
	CHECK ((service_account IS NULL) = (decided_at IS NULL)),
	CHECK ((code_hash IS NULL) = (code_expires_at IS NULL))
);

CREATE TABLE oauth_refresh_family (
	id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
	authorization_id uuid NOT NULL UNIQUE REFERENCES oauth_authorization (id),
	revoked_at timestamptz
);

CREATE TABLE oauth_token (
	token_hash bytea PRIMARY KEY,
	family_id uuid NOT NULL REFERENCES oauth_refresh_family (id),
	kind text NOT NULL CHECK (kind IN ('access', 'refresh')),
	expires_at timestamptz NOT NULL,
	rotated_at timestamptz
);
`
