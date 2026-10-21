package linkage

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"log/slog"
	"maps"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/sethvargo/go-retry"
	"golang.org/x/oauth2"
)

const (
	// refreshSkew is how long before its expiry a token is refreshed.
	refreshSkew = time.Minute
	// claimTTL is how long a replica's claim on a token keeps the other replicas from refreshing it.
	claimTTL = 30 * time.Second
	// sweepInterval is how often Run refreshes the tokens that are due.
	sweepInterval = 30 * time.Second
	// sweepLock is the PostgreSQL advisory lock a sweep holds, the Python service's too: "AGPMCREF".
	sweepLock int64 = 0x4147504D43524546
	// firstRetryDelay and maxRetryDelay pace the retries of a refresh that may pass: doubling from the first.
	firstRetryDelay = 30 * time.Second
	maxRetryDelay   = 15 * time.Minute
)

// Token is what the server's linkage holds now, refreshed first when it is due; nil when no account is linked to it.
// A refresh that fails is recorded on the linkage, not returned.
func (a *Authority) Token(ctx context.Context, serverID string) (*Token, error) {
	server, err := a.server(serverID)
	if err != nil {
		return nil, err
	}
	if err := a.refreshIfDue(ctx, server); err != nil {
		return nil, err
	}
	var token Token
	var action *string
	err = a.db.QueryRowContext(
		ctx, `
		SELECT token.access_token, token.expires_at, token.refresh_failure_action
		FROM mcp_server_linkage AS linkage
		JOIN mcp_oauth_token_state AS token ON token.id = linkage.token_state_id
		WHERE linkage.server_id = $1`, serverID,
	).Scan(&token.AccessToken, &token.ExpiresAt, &action)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if token.RefreshFailure, err = parseAction(action); err != nil {
		return nil, err
	}
	return &token, nil
}

// Run refreshes the tokens that are due every sweepInterval until ctx ends, on one replica at a time: the one that
// holds the sweep's advisory lock. A token is also refreshed when Token reads it.
func (a *Authority) Run(ctx context.Context) {
	sweeps := time.NewTicker(sweepInterval)
	defer sweeps.Stop()
	for {
		if err := a.sweep(ctx); err != nil && ctx.Err() == nil {
			slog.WarnContext(ctx, "MCP OAuth refresh sweep failed; retrying next sweep", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-sweeps.C:
		}
	}
}

func (a *Authority) sweep(ctx context.Context) (err error) {
	// The lock is the session's: it is taken and released on one connection.
	conn, err := a.db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	var locked bool
	if err := conn.QueryRowContext(ctx, "SELECT pg_try_advisory_lock($1)", sweepLock).Scan(&locked); err != nil || !locked {
		return err
	}
	defer func() {
		if _, unlockErr := conn.ExecContext(context.WithoutCancel(ctx), "SELECT pg_advisory_unlock($1)", sweepLock); unlockErr != nil {
			// The session would keep the lock in the pool: discard the connection, which ends the session.
			_ = conn.Raw(func(any) error { return driver.ErrBadConn })
			err = errors.Join(err, unlockErr)
		}
	}()
	for _, serverID := range slices.Sorted(maps.Keys(a.servers)) {
		if err := a.refreshIfDue(ctx, a.servers[serverID]); err != nil {
			return err
		}
	}
	return nil
}

// claim is a replica's claim on a token it refreshes.
type claim struct {
	id, stateID uuid.UUID
	// revision and refreshToken are the token's when claimed: a relink meanwhile replaces them, and the refresh
	// is then dropped.
	revision      int32
	refreshToken  string
	tokenEndpoint *string
}

// refreshIfDue refreshes the server's token when it is due and no replica holds a claim on it, and records the
// outcome. A token is due within refreshSkew of its expiry, unless the provider refused its last refresh or the next
// retry is not due yet. A token without an expiry is never refreshed. It fails only when the database does.
func (a *Authority) refreshIfDue(ctx context.Context, server Server) error {
	at := now()
	var c claim
	err := a.db.QueryRowContext(
		ctx, `
		UPDATE mcp_oauth_token_state AS token SET refresh_claim_id = $2, refresh_claim_expires_at = $4
		FROM mcp_server_linkage AS linkage
		WHERE linkage.server_id = $1 AND token.id = linkage.token_state_id
			AND token.refresh_token <> '' AND token.expires_at <= $5
			AND (token.refresh_failure_action IS NULL OR token.refresh_failure_action = $6)
			AND (token.refresh_retry_at IS NULL OR token.refresh_retry_at <= $3)
			AND (token.refresh_claim_expires_at IS NULL OR token.refresh_claim_expires_at <= $3)
		RETURNING token.refresh_claim_id, token.id, token.token_revision, token.refresh_token, linkage.token_endpoint`,
		server.ServerID, uuid.New(), at, at.Add(claimTTL), at.Add(refreshSkew), string(RefreshRetrying),
	).Scan(&c.id, &c.stateID, &c.revision, &c.refreshToken, &c.tokenEndpoint)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	// A refresh token may be spent once the request is sent: the outcome is stored even when the caller stops
	// waiting.
	ctx = context.WithoutCancel(ctx)
	token, err := a.refresh(ctx, server, c)
	var failure *tokenError
	if errors.As(err, &failure) {
		return a.storeFailure(ctx, server, c, failure)
	}
	if err != nil {
		return err
	}
	return a.storeRefreshed(ctx, c, token)
}

func (a *Authority) refresh(ctx context.Context, server Server, c claim) (*oauth2.Token, error) {
	if c.tokenEndpoint == nil {
		return nil, &tokenError{action: RefreshReconnect, message: "no token endpoint was discovered for this server"}
	}
	return a.requestToken(ctx, "refresh", server, *c.tokenEndpoint,
		func(ctx context.Context, config *oauth2.Config) (*oauth2.Token, error) {
			return config.TokenSource(ctx, &oauth2.Token{RefreshToken: c.refreshToken}).Token()
		})
}

// storeRefreshed replaces the claimed token with the refreshed one, unless the claim was lost meanwhile.
func (a *Authority) storeRefreshed(ctx context.Context, c claim, token *oauth2.Token) error {
	return a.transaction(ctx, func(tx *sql.Tx) error {
		result, err := tx.ExecContext(ctx, `
			UPDATE mcp_oauth_token_state SET access_token = $5, refresh_token = COALESCE($6, refresh_token),
				expires_at = $7, token_revision = token_revision + 1, refresh_claim_id = NULL,
				refresh_claim_expires_at = NULL, refresh_failure_count = 0, refresh_failure_action = NULL,
				refresh_retry_at = NULL, refresh_failure_error = NULL
			WHERE id = $1 AND refresh_claim_id = $2 AND token_revision = $3 AND refresh_token = $4`,
			c.stateID, c.id, c.revision, c.refreshToken, token.AccessToken, refreshToken(token), expiry(token))
		if err != nil {
			return err
		}
		if stored, err := result.RowsAffected(); err != nil || stored == 0 {
			return err
		}
		return notify(ctx, tx)
	})
}

// storeFailure records the failed refresh on the claimed token, unless the claim was lost meanwhile: what the
// provider answered, and when to try again unless the account must be linked again.
func (a *Authority) storeFailure(ctx context.Context, server Server, c claim, failure *tokenError) error {
	stored := false
	err := a.transaction(ctx, func(tx *sql.Tx) error {
		var failures int
		err := tx.QueryRowContext(
			ctx, `
			SELECT refresh_failure_count FROM mcp_oauth_token_state WHERE id = $1 AND refresh_claim_id = $2 FOR UPDATE`,
			c.stateID, c.id,
		).Scan(&failures)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		failures++
		var retryAt *time.Time
		if failure.action == RefreshRetrying {
			retryAt = new(now().Add(retryDelay(failures)))
		}
		if _, err := tx.ExecContext(
			ctx, `
			UPDATE mcp_oauth_token_state SET refresh_failure_count = $2, refresh_failure_action = $3,
				refresh_failure_error = $4, refresh_retry_at = $5, refresh_claim_id = NULL, refresh_claim_expires_at = NULL
			WHERE id = $1`, c.stateID, failures, string(failure.action), failure.message, retryAt,
		); err != nil {
			return err
		}
		stored = true
		return notify(ctx, tx)
	})
	if stored && err == nil {
		slog.WarnContext(ctx, "MCP OAuth token refresh failed", "server", server.ServerID, "action", failure.action,
			"error", failure.message)
	}
	return err
}

// retryDelay is how long after its attempts-th failure a refresh that may pass is tried again.
func retryDelay(attempts int) time.Duration {
	backoff := retry.WithCappedDuration(maxRetryDelay, retry.NewExponential(firstRetryDelay))
	var delay time.Duration
	for range attempts {
		delay, _ = backoff.Next()
	}
	return delay
}
