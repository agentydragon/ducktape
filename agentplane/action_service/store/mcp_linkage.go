package store

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

// MCPLinkage is the token an operator's OAuth linkage to an MCP server holds now, which the MCP executor sends the
// server. The linkage flow that writes it is the Python service's until it is ported.
type MCPLinkage struct {
	AccessToken string
	// ExpiresAt is when AccessToken lapses; nil when the provider gave no expiry.
	ExpiresAt *time.Time
	// RefreshFailureAction is what the last failed refresh asks for: `retrying`, `reconnect` or `operator_action`;
	// nil when the last refresh did not fail.
	RefreshFailureAction *string
}

// MCPLinkage reads the linkage of the MCP server serverID; nil when no account is linked to it.
func (s *Store) MCPLinkage(ctx context.Context, serverID string) (*MCPLinkage, error) {
	var linkage MCPLinkage
	err := s.db.QueryRowContext(
		ctx, `
		SELECT token.access_token, token.expires_at, token.refresh_failure_action
		FROM mcp_server_linkage AS linkage
		JOIN mcp_oauth_token_state AS token ON token.id = linkage.token_state_id
		WHERE linkage.server_id = $1`, serverID,
	).Scan(&linkage.AccessToken, &linkage.ExpiresAt, &linkage.RefreshFailureAction)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &linkage, nil
}
