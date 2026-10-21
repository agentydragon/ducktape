package linkage

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"golang.org/x/oauth2"
)

var tokenRequestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
	Name: "agentplane_mcp_oauth_token_request_duration_seconds",
	Help: "MCP OAuth discovery and token endpoint request duration",
}, []string{"operation", "outcome"})

func observe(operation, outcome string, started time.Time) {
	tokenRequestDuration.WithLabelValues(operation, outcome).Observe(time.Since(started).Seconds())
}

// Start begins linking an account to the server for the operator, whom the flow records as the table requires: it
// discovers the server's OAuth metadata and answers with the authorization URL the operator's browser opens. requested
// are the scopes to request, each one the server allows; empty requests all of those.
func (a *Authority) Start(ctx context.Context, serverID string, requested []string, operator store.OperatorPrincipal) (*operator_action_servicepb.StartMcpServerLinkageResponse, error) {
	server, err := a.server(serverID)
	if err != nil {
		return nil, err
	}
	found, err := a.discover(ctx, server)
	if err != nil {
		return nil, err
	}
	if server.UseSharedCIMD && !found.cimd {
		return nil, &UnusableError{Reason: "MCP OAuth metadata does not advertise Client ID Metadata Document support"}
	}
	scopes, err := chooseScopes(requested, found.scopes)
	if err != nil {
		return nil, err
	}
	state, verifier := rand.Text(), oauth2.GenerateVerifier()
	authorization, err := a.authorizationURL(server, found, scopes, state, verifier)
	if err != nil {
		return nil, err
	}
	stored, err := scopesJSON(scopes)
	if err != nil {
		return nil, err
	}
	if _, err := a.db.ExecContext(
		ctx, `
		INSERT INTO mcp_linkage_flow (id, server_id, state_hash, verifier, scopes, authorization_endpoint, token_endpoint,
			resource, expires_at, operator_issuer, operator_subject)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
		uuid.New(), serverID, digest(state), verifier, stored, found.authorizationEndpoint, found.tokenEndpoint,
		found.resource, now().Add(flowLifetime), operator.Issuer, operator.Subject,
	); err != nil {
		return nil, err
	}
	return &operator_action_servicepb.StartMcpServerLinkageResponse{AuthorizationUrl: authorization}, nil
}

// chooseScopes are the requested scopes once each, in order, or all the allowed ones when none is requested.
func chooseScopes(requested, allowed []string) ([]string, error) {
	if len(requested) == 0 {
		requested = allowed
	}
	var scopes []string
	for _, scope := range requested {
		if !slices.Contains(allowed, scope) {
			return nil, ErrUnconfiguredScopes
		}
		if !slices.Contains(scopes, scope) {
			scopes = append(scopes, scope)
		}
	}
	return scopes, nil
}

// authorizationURL is the authorization endpoint with the flow's PKCE authorization request in its query, which
// keeps the endpoint's own parameters but for those the request sets.
func (a *Authority) authorizationURL(server Server, found discovered, scopes []string, state, verifier string) (string, error) {
	endpoint, err := url.Parse(found.authorizationEndpoint)
	if err != nil {
		return "", err
	}
	query := endpoint.Query()
	endpoint.RawQuery = ""
	config := oauth2.Config{
		ClientID: a.clientID(server), Endpoint: oauth2.Endpoint{AuthURL: endpoint.String()}, RedirectURL: server.RedirectURI,
		Scopes: scopes,
	}
	authorization, err := url.Parse(config.AuthCodeURL(state, oauth2.S256ChallengeOption(verifier),
		oauth2.SetAuthURLParam("resource", found.resource)))
	if err != nil {
		return "", err
	}
	maps.Copy(query, authorization.Query())
	authorization.RawQuery = query.Encode()
	return authorization.String(), nil
}

func digest(state string) string {
	sum := sha256.Sum256([]byte(state))
	return hex.EncodeToString(sum[:])
}

// flow is a started link, as its callback reads it.
type flow struct {
	verifier      string
	scopes        []string
	tokenEndpoint string
	resource      *string
}

// Callback finishes the flow state names with the authorization code: it spends the flow, exchanges the code, and
// answers with the server's linkage, which holds the new token. A flow is spent even when the exchange fails.
func (a *Authority) Callback(ctx context.Context, state, code string) (*operator_action_servicepb.McpServerLinkage, error) {
	server, f, err := a.spend(ctx, state)
	if err != nil {
		return nil, err
	}
	token, err := a.requestToken(ctx, "exchange", server, f.tokenEndpoint,
		func(ctx context.Context, config *oauth2.Config) (*oauth2.Token, error) {
			options := []oauth2.AuthCodeOption{oauth2.VerifierOption(f.verifier)}
			if len(f.scopes) > 0 {
				options = append(options, oauth2.SetAuthURLParam("scope", strings.Join(f.scopes, " ")))
			}
			if f.resource != nil {
				options = append(options, oauth2.SetAuthURLParam("resource", *f.resource))
			}
			return config.Exchange(ctx, code, options...)
		})
	if err != nil {
		return nil, err
	}
	return a.link(ctx, server, f, token)
}

// spend marks the flow state names consumed, unless it is unknown, spent or expired, or its server is no longer
// configured.
func (a *Authority) spend(ctx context.Context, state string) (Server, flow, error) {
	var server Server
	var f flow
	err := a.transaction(ctx, func(tx *sql.Tx) error {
		var id uuid.UUID
		var serverID string
		var scopes []byte
		var expiresAt time.Time
		var consumedAt *time.Time
		err := tx.QueryRowContext(
			ctx, `
			SELECT id, server_id, verifier, scopes, token_endpoint, resource, expires_at, consumed_at
			FROM mcp_linkage_flow WHERE state_hash = $1 FOR UPDATE`, digest(state),
		).Scan(&id, &serverID, &f.verifier, &scopes, &f.tokenEndpoint, &f.resource, &expiresAt, &consumedAt)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			return ErrInvalidFlow
		case err != nil:
			return err
		case consumedAt != nil || !expiresAt.After(time.Now()):
			return ErrInvalidFlow
		}
		if server, err = a.server(serverID); err != nil {
			return err
		}
		if err := json.Unmarshal(scopes, &f.scopes); err != nil {
			return fmt.Errorf("stored scopes of flow %s: %w", id, err)
		}
		_, err = tx.ExecContext(ctx, "UPDATE mcp_linkage_flow SET consumed_at = $2 WHERE id = $1", id, now())
		return err
	})
	return server, f, err
}

// link stores the token as the server's and answers with the server's linkage. A new token's revision is 1; a relink
// increases it, which fences a refresh claimed before it. Columns nothing reads (the token type, updated_at, the
// linkage's server_url and revision) are written only where the table requires a value.
func (a *Authority) link(ctx context.Context, server Server, f flow, token *oauth2.Token) (*operator_action_servicepb.McpServerLinkage, error) {
	scopes, err := scopesJSON(f.scopes)
	if err != nil {
		return nil, err
	}
	var views []*operator_action_servicepb.McpServerLinkage
	err = a.transaction(ctx, func(tx *sql.Tx) error {
		var stateID uuid.UUID
		if err := tx.QueryRowContext(
			ctx, `
			INSERT INTO mcp_oauth_token_state (id, server_id, access_token, refresh_token, token_type, scope, expires_at,
				token_revision, updated_at, refresh_failure_count)
			VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8, 0)
			ON CONFLICT (server_id) DO UPDATE SET access_token = excluded.access_token,
				refresh_token = COALESCE(excluded.refresh_token, mcp_oauth_token_state.refresh_token),
				scope = excluded.scope, expires_at = excluded.expires_at,
				token_revision = mcp_oauth_token_state.token_revision + 1, refresh_claim_id = NULL,
				refresh_claim_expires_at = NULL, refresh_failure_count = 0, refresh_failure_action = NULL,
				refresh_retry_at = NULL, refresh_failure_error = NULL
			RETURNING id`,
			uuid.New(), server.ServerID, token.AccessToken, refreshToken(token), token.Type(), scopes, expiry(token), now(),
		).Scan(&stateID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(
			ctx, `
			INSERT INTO mcp_server_linkage (server_id, server_url, revision, scopes, token_state_id, token_endpoint, resource)
			VALUES ($1, $2, 1, $3, $4, $5, $6)
			ON CONFLICT (server_id) DO UPDATE SET scopes = excluded.scopes, token_state_id = excluded.token_state_id,
				token_endpoint = excluded.token_endpoint, resource = excluded.resource`,
			server.ServerID, server.ServerURL, scopes, stateID, f.tokenEndpoint, f.resource,
		); err != nil {
			return err
		}
		if err := notify(ctx, tx); err != nil {
			return err
		}
		views, _, err = a.views(ctx, tx, []string{server.ServerID})
		return err
	})
	if err != nil {
		return nil, err
	}
	return views[0], nil
}

// refreshToken is the token's refresh token; nil when it carries none, so a stored one is kept.
func refreshToken(token *oauth2.Token) *string {
	if token.RefreshToken == "" {
		return nil
	}
	return &token.RefreshToken
}

// expiry is when the token lapses; nil when the provider gave no expiry.
func expiry(token *oauth2.Token) *time.Time {
	if token.Expiry.IsZero() {
		return nil
	}
	expires := token.Expiry.UTC().Truncate(time.Microsecond)
	return &expires
}

// tokenError is a token request the token endpoint did not grant.
type tokenError struct {
	action RefreshAction
	// message says what the endpoint answered, for an operator; it names neither a token nor the client secret.
	message string
}

func (e *tokenError) Error() string { return e.message }

// requestToken runs request against the server's token endpoint, as the server's client and over an HTTP client that
// follows no redirect. A token endpoint that does not grant a token fails with a *tokenError.
func (a *Authority) requestToken(
	ctx context.Context, operation string, server Server, tokenEndpoint string,
	request func(context.Context, *oauth2.Config) (*oauth2.Token, error),
) (*oauth2.Token, error) {
	var secret string
	if server.ClientSecretFile != nil {
		read, err := os.ReadFile(*server.ClientSecretFile)
		if err != nil {
			slog.ErrorContext(ctx, "MCP OAuth client secret unreadable", "server", server.ServerID, "error", err)
			return nil, &tokenError{action: RefreshRetrying, message: "the client secret could not be read"}
		}
		secret = strings.TrimSpace(string(read))
	}
	config := &oauth2.Config{
		ClientID: a.clientID(server), ClientSecret: secret, RedirectURL: server.RedirectURI,
		Endpoint: oauth2.Endpoint{TokenURL: tokenEndpoint, AuthStyle: oauth2.AuthStyleInParams},
	}
	started := time.Now()
	token, err := request(context.WithValue(ctx, oauth2.HTTPClient, a.tokens), config)
	if err != nil {
		failure, outcome := tokenFailure(err)
		observe(operation, outcome, started)
		return nil, failure
	}
	observe(operation, "success", started)
	return token, nil
}

// tokenFailure classifies a failed token request as the Python service does, with its metric outcome: the provider
// refused it (HTTP 400, or an OAuth error in a 2xx answer, as GitHub reports a bad code) and the account must be
// linked again, or it may pass on a retry.
func tokenFailure(err error) (*tokenError, string) {
	var refused *oauth2.RetrieveError
	var unreachable *url.Error
	switch {
	// x/oauth2 reports a 2xx answer as refused only when it carries an OAuth error.
	case errors.As(err, &refused) && (refused.Response.StatusCode == http.StatusBadRequest || refused.Response.StatusCode < 300):
		reason := "HTTP " + refused.Response.Status
		if refused.ErrorCode != "" {
			reason = refused.ErrorCode
			if refused.ErrorDescription != "" {
				reason += ": " + refused.ErrorDescription
			}
		}
		return &tokenError{action: RefreshReconnect, message: "the OAuth provider refused the token request: " + reason}, "rejected"
	case errors.As(err, &refused):
		return &tokenError{action: RefreshRetrying, message: "the token endpoint answered HTTP " + refused.Response.Status}, "transport"
	case errors.As(err, &unreachable):
		return &tokenError{action: RefreshRetrying, message: "the token endpoint was unreachable: " + unreachable.Err.Error()}, "transport"
	default:
		// An answer x/oauth2 cannot read, or one without an access token.
		return &tokenError{action: RefreshRetrying, message: "the token endpoint's answer held no token: " + err.Error()}, "invalid_response"
	}
}
