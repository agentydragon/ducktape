// Package linkage holds the OAuth linkages operators make to the configured MCP servers, in the Action Service's
// PostgreSQL database: the authorization code flow that links an account, its public callback and the shared Client
// ID Metadata Document, the refresh of each linked token, and the token the MCP executor sends a server. One token
// family serves each server, whichever replica asks: a refresh claims the token first, so one replica at a time
// spends its refresh token.
//
// Every commit that changes a linkage or its token notifies UpdatesChannel, with no payload.
package linkage

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net/http"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/lib/pq"
	"github.com/modelcontextprotocol/go-sdk/oauthex"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	// UpdatesChannel is notified, with no payload, by every commit that changes a linkage or its token.
	UpdatesChannel = "agentplane_mcp_linkage_updates"
	// ClientMetadataPath serves the shared Client ID Metadata Document.
	ClientMetadataPath = "/oauth/client-metadata.json"
	// CallbackPath is where an operator's browser returns from the authorization server.
	CallbackPath = "/v1/mcp-linkage/callback"

	// flowLifetime is how long a started link waits for its callback.
	flowLifetime = 10 * time.Minute
	// discoveryTimeout and tokenTimeout bound one request to a server's metadata and to a token endpoint.
	discoveryTimeout = 10 * time.Second
	tokenTimeout     = 15 * time.Second
)

// Errors a caller can act on. Every other failure is the database's, or a stored row no message can show.
var (
	ErrUnknownServer      = errors.New("unknown MCP server")
	ErrUnconfiguredScopes = errors.New("requested OAuth scopes are not configured for this MCP server")
	// ErrInvalidFlow is a callback whose state names no flow, or one that finished or expired.
	ErrInvalidFlow = errors.New("OAuth linkage flow is invalid or expired")
)

// UnusableError is a server whose OAuth metadata cannot start a link.
type UnusableError struct{ Reason string }

func (e *UnusableError) Error() string { return e.Reason }

// RefreshAction is what a failed refresh asks for, stored as `mcp_oauth_token_state.refresh_failure_action`.
type RefreshAction string

const (
	// RefreshReconnect: the provider refused the refresh, and an operator links the account again.
	RefreshReconnect RefreshAction = "reconnect"
	// RefreshRetrying: the refresh failed in a way that may pass, and the service tries again.
	RefreshRetrying RefreshAction = "retrying"
)

// parseAction reads a stored action. An earlier release of the Python service stored `operator_action` for what is
// now `reconnect`, and the Python service still reads it so.
func parseAction(stored *string) (*RefreshAction, error) {
	if stored == nil {
		return nil, nil
	}
	var action RefreshAction
	switch *stored {
	case string(RefreshReconnect), "operator_action":
		action = RefreshReconnect
	case string(RefreshRetrying):
		action = RefreshRetrying
	default:
		return nil, fmt.Errorf("stored refresh failure action %q is not one this service knows", *stored)
	}
	return &action, nil
}

var refreshActions = map[RefreshAction]operator_action_servicepb.McpRefreshAction{
	RefreshReconnect: operator_action_servicepb.McpRefreshAction_MCP_REFRESH_ACTION_RECONNECT,
	RefreshRetrying:  operator_action_servicepb.McpRefreshAction_MCP_REFRESH_ACTION_RETRYING,
}

// Token is what a server's linkage holds now, which the MCP executor sends the server.
type Token struct {
	AccessToken string
	// ExpiresAt is when AccessToken lapses; nil when the provider gave no expiry.
	ExpiresAt *time.Time
	// RefreshFailure is what the latest refresh asks for; nil when it did not fail.
	RefreshFailure *RefreshAction
}

// Authority links operators' accounts to the configured MCP servers, refreshes their tokens and serves them.
type Authority struct {
	db      *sql.DB
	servers map[string]Server
	// sharedClientID is the shared Client ID Metadata Document's URL; empty when no server uses it.
	sharedClientID string
	// document is the shared Client ID Metadata Document, as served; nil when no server uses it.
	document []byte
	// discovery and tokens reach the servers and their authorization servers, and follow no redirect.
	discovery, tokens *http.Client
}

// New is the Authority of the servers, over the database db. servers and metadata must pass Validate.
func New(db *sql.DB, servers map[string]Server, metadata ClientMetadata) (*Authority, error) {
	a := &Authority{
		db: db, servers: servers, sharedClientID: metadata.URL,
		discovery: httpClient(discoveryTimeout), tokens: httpClient(tokenTimeout),
	}
	if metadata == (ClientMetadata{}) {
		return a, nil
	}
	redirects := map[string]bool{}
	for _, server := range servers {
		if server.UseSharedCIMD {
			redirects[server.RedirectURI] = true
		}
	}
	document, err := json.Marshal(struct {
		ClientID string `json:"client_id"`
		oauthex.ClientRegistrationMetadata
	}{metadata.URL, oauthex.ClientRegistrationMetadata{
		RedirectURIs: slices.Sorted(maps.Keys(redirects)), TokenEndpointAuthMethod: "none",
		GrantTypes: []string{"authorization_code", "refresh_token"}, ResponseTypes: []string{"code"},
		ClientName: metadata.ClientName,
	}})
	if err != nil {
		return nil, err
	}
	a.document = document
	return a, nil
}

func httpClient(timeout time.Duration) *http.Client {
	return &http.Client{
		// http.DefaultTransport dials as the process does: a server or its authorization server may be in the
		// cluster, at a private address.
		Transport:     http.DefaultTransport,
		Timeout:       timeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func (a *Authority) server(serverID string) (Server, error) {
	server, configured := a.servers[serverID]
	if !configured {
		return Server{}, ErrUnknownServer
	}
	return server, nil
}

// clientID is the client the server's authorization server knows this service as.
func (a *Authority) clientID(server Server) string {
	if server.UseSharedCIMD {
		return a.sharedClientID
	}
	return *server.ClientID
}

// now is the time a write records: PostgreSQL keeps microseconds, so a value read back equals the one written.
func now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

func (a *Authority) transaction(ctx context.Context, f func(*sql.Tx) error) error {
	tx, err := a.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := f(tx); err != nil {
		return err
	}
	return tx.Commit()
}

func notify(ctx context.Context, tx *sql.Tx) error {
	_, err := tx.ExecContext(ctx, "SELECT pg_notify($1, '')", UpdatesChannel)
	return err
}

// scopesJSON is scopes as a JSONB column holds them: a list, never null.
func scopesJSON(scopes []string) ([]byte, error) {
	if scopes == nil {
		scopes = []string{}
	}
	return json.Marshal(scopes)
}

// Linkages are the linkages of the configured servers, by server ID.
func (a *Authority) Linkages(ctx context.Context) ([]*operator_action_servicepb.McpServerLinkage, error) {
	var views []*operator_action_servicepb.McpServerLinkage
	err := a.read(ctx, func(tx *sql.Tx) error {
		var err error
		views, err = a.views(ctx, tx, slices.Sorted(maps.Keys(a.servers)))
		return err
	})
	return views, err
}

// Linkage is the linkage of the configured server serverID.
func (a *Authority) Linkage(ctx context.Context, serverID string) (*operator_action_servicepb.McpServerLinkage, error) {
	if _, err := a.server(serverID); err != nil {
		return nil, err
	}
	var views []*operator_action_servicepb.McpServerLinkage
	err := a.read(ctx, func(tx *sql.Tx) error {
		var err error
		views, err = a.views(ctx, tx, []string{serverID})
		return err
	})
	if err != nil {
		return nil, err
	}
	return views[0], nil
}

// read runs f on one snapshot, so a linkage and its token agree.
func (a *Authority) read(ctx context.Context, f func(*sql.Tx) error) error {
	tx, err := a.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return err
	}
	defer tx.Rollback()
	return f(tx)
}

// stored is a server's linkage row, and the token it holds.
type stored struct {
	revision        int32
	scopes          []string
	linkedAt        *time.Time
	linkedByIssuer  *string
	linkedBySubject *string
	// linked is whether the linkage holds a token; the fields below are the token's.
	linked        bool
	expiresAt     *time.Time
	failureAction *string
	failureError  *string
	failures      *int32
	retryAt       *time.Time
}

// views are the linkages of the configured servers serverIDs, in their order.
func (a *Authority) views(ctx context.Context, tx *sql.Tx, serverIDs []string) ([]*operator_action_servicepb.McpServerLinkage, error) {
	rows, err := tx.QueryContext(ctx, `
		SELECT linkage.server_id, linkage.revision, linkage.scopes, linkage.linked_at, linkage.linked_by_issuer,
			linkage.linked_by_subject, token.id IS NOT NULL, token.expires_at, token.refresh_failure_action,
			token.refresh_failure_error, token.refresh_failure_count, token.refresh_retry_at
		FROM mcp_server_linkage AS linkage
		LEFT JOIN mcp_oauth_token_state AS token ON token.id = linkage.token_state_id
		WHERE linkage.server_id = ANY($1)`, pq.Array(serverIDs))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	found := map[string]*stored{}
	for rows.Next() {
		var serverID string
		var scopes []byte
		var row stored
		if err := rows.Scan(&serverID, &row.revision, &scopes, &row.linkedAt, &row.linkedByIssuer, &row.linkedBySubject,
			&row.linked, &row.expiresAt, &row.failureAction, &row.failureError, &row.failures, &row.retryAt); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(scopes, &row.scopes); err != nil {
			return nil, fmt.Errorf("stored scopes of MCP server %s: %w", serverID, err)
		}
		found[serverID] = &row
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	at := time.Now()
	views := make([]*operator_action_servicepb.McpServerLinkage, len(serverIDs))
	for i, serverID := range serverIDs {
		if views[i], err = view(a.servers[serverID], found[serverID], at); err != nil {
			return nil, err
		}
	}
	return views, nil
}

// view is the server's linkage at the time at, from its row, which is nil when the server never had a linkage.
func view(server Server, row *stored, at time.Time) (*operator_action_servicepb.McpServerLinkage, error) {
	v := &operator_action_servicepb.McpServerLinkage{
		ServerId: server.ServerID, ServerUrl: server.ServerURL, Status: operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_UNLINKED,
		Scopes: server.Scopes,
	}
	if row == nil {
		return v, nil
	}
	v.Revision, v.Scopes = row.revision, row.scopes
	if !row.linked {
		return v, nil
	}
	action, err := parseAction(row.failureAction)
	if err != nil {
		return nil, err
	}
	switch {
	case action != nil && *action == RefreshReconnect:
		v.Status = operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_DEGRADED
	case row.expiresAt != nil && !row.expiresAt.After(at):
		v.Status = operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_EXPIRED
	default:
		v.Status = operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED
	}
	v.ExpiresAt, v.LinkedAt = timestamp(row.expiresAt), timestamp(row.linkedAt)
	if row.linkedByIssuer != nil && row.linkedBySubject != nil {
		v.LinkedBy = &action_servicepb.Operator{Issuer: *row.linkedByIssuer, Subject: *row.linkedBySubject}
	}
	// A failure still being retried is shown too: the token may lapse before it clears. The table's CHECK sets the
	// action and the error together.
	if action != nil && row.failureError != nil {
		v.RefreshFailure = &operator_action_servicepb.McpRefreshFailure{
			Action: refreshActions[*action], Error: *row.failureError, Attempts: *row.failures, RetryAt: timestamp(row.retryAt),
		}
	}
	return v, nil
}

func timestamp(t *time.Time) *timestamppb.Timestamp {
	if t == nil {
		return nil
	}
	return timestamppb.New(*t)
}

// Disconnect discards the server's token and answers with its now unlinked linkage. Each disconnection of a server
// that has had a linkage increases its revision, as in the Python service.
func (a *Authority) Disconnect(ctx context.Context, serverID string) (*operator_action_servicepb.McpServerLinkage, error) {
	server, err := a.server(serverID)
	if err != nil {
		return nil, err
	}
	scopes, err := scopesJSON(server.Scopes)
	if err != nil {
		return nil, err
	}
	var views []*operator_action_servicepb.McpServerLinkage
	err = a.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `
			UPDATE mcp_server_linkage SET revision = revision + 1, token_state_id = NULL, scopes = $2, linked_at = NULL,
				linked_by_issuer = NULL, linked_by_subject = NULL
			WHERE server_id = $1`, serverID, scopes); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "DELETE FROM mcp_oauth_token_state WHERE server_id = $1", serverID); err != nil {
			return err
		}
		if err := notify(ctx, tx); err != nil {
			return err
		}
		views, err = a.views(ctx, tx, []string{serverID})
		return err
	})
	if err != nil {
		return nil, err
	}
	return views[0], nil
}

// RemoveUnconfigured deletes the linkages, tokens and flows of servers no longer configured. With none configured it
// keeps everything, as the Python service does.
func (a *Authority) RemoveUnconfigured(ctx context.Context) error {
	if len(a.servers) == 0 {
		return nil
	}
	configured := pq.Array(slices.Collect(maps.Keys(a.servers)))
	return a.transaction(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "DELETE FROM mcp_linkage_flow WHERE NOT (server_id = ANY($1))", configured); err != nil {
			return err
		}
		var removed int64
		for _, table := range []string{"mcp_server_linkage", "mcp_oauth_token_state"} {
			result, err := tx.ExecContext(ctx, "DELETE FROM "+table+" WHERE NOT (server_id = ANY($1))", configured)
			if err != nil {
				return err
			}
			deleted, err := result.RowsAffected()
			if err != nil {
				return err
			}
			removed += deleted
		}
		if removed == 0 {
			return nil
		}
		return notify(ctx, tx)
	})
}
