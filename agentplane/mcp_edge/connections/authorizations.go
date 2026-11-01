package connections

import (
	"context"
	"crypto/rand"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
)

// The authorization server's side of a consent: it creates the enrollment, turns an allowed one into an active grant
// at its code exchange, resolves that grant on every bearer admission, and revokes it. Each runs in the authorization
// server's transaction, so its own rows commit or roll back with these.

// Refusal is a check that refused the authorization server: a fixed sentence naming the check, which the
// authorization server tells its client. Any other error these return is a fault.
type Refusal string

func (r Refusal) Error() string { return string(r) }

const (
	refusedReused      Refusal = "authorization correlation was already used; restart with fresh PKCE"
	refusedUnknown     Refusal = "enrollment not found"
	refusedExpired     Refusal = "enrollment expired; restart authorization"
	refusedNotApproved Refusal = "enrollment was not approved"
	refusedOperator    Refusal = "issuing operator does not match consent"
	refusedClaimed     Refusal = "token exchange already claimed; restart authorization"
	refusedChanged     Refusal = "Connection changed; restart authorization and review it again"
	refusedCaller      Refusal = notLabeled
	refusedGrant       Refusal = "grant is not authorized"
	refusedUnbound     Refusal = "connection has no bound caller"
)

// enrollmentWindow is how long an authorization has for consent, the upstream sign-in and the code exchange.
const enrollmentWindow = 15 * time.Minute

// NewEnrollment is an authorization that asks for consent.
type NewEnrollment struct {
	// ID is also the id of the grant an allowed consent becomes.
	ID               uuid.UUID
	Issuer, ClientID string
	// ClientName is the name the client gives itself, if any.
	ClientName                 *string
	RedirectURI, CodeChallenge string
	// UpstreamURL is where an allowed consent sends the browser next.
	UpstreamURL string
}

// CreateEnrollment records the enrollment and returns the handle its consent URL carries and when it expires. An
// enrollment of the client, redirect URI and code challenge of an earlier one is refused.
func (s *Store) CreateEnrollment(ctx context.Context, tx *sql.Tx, e NewEnrollment) (handle string, expiresAt time.Time, err error) {
	handle, expiresAt = rand.Text(), now().Add(enrollmentWindow)
	var created uuid.UUID
	err = tx.QueryRowContext(
		ctx, `
		INSERT INTO connection_enrollment (id, handle_hash, issuer, client_id, client_name, redirect_uri, code_challenge,
			upstream_url, expires_at, version)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 1)
		ON CONFLICT DO NOTHING RETURNING id`,
		e.ID, digest(handle), e.Issuer, e.ClientID, e.ClientName, e.RedirectURI, e.CodeChallenge, e.UpstreamURL, expiresAt,
	).Scan(&created)
	if errors.Is(err, sql.ErrNoRows) {
		return "", time.Time{}, refusedReused
	}
	return handle, expiresAt, err
}

// Activate turns the allowed enrollment into the active grant of the same id, once: it creates the Connection the
// consent named, or replaces the authority of the one it reconnects, and claims the enrollment so it issues no
// second grant. The consent must be live, decided by the approver, and for an account still labeled.
func (s *Store) Activate(ctx context.Context, tx *sql.Tx, id uuid.UUID, approver Operator) error {
	e, err := lockEnrollmentWhere(ctx, tx, "id", id)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return refusedUnknown
	case err != nil:
		return err
	case !e.expiresAt.After(time.Now()):
		return refusedExpired
	case e.verdict == nil || *e.verdict != verdictAllow || e.allowed == nil:
		return refusedNotApproved
	case e.operator == nil || *e.operator != approver:
		return refusedOperator
	case e.claimedAt != nil:
		return refusedClaimed
	case !s.labeled(e.allowed.account):
		return refusedCaller
	}
	activatedAt := now()
	g := grant{
		id: id, caller: e.allowed.account, issuer: e.issuer, clientID: e.clientID, status: grantActive,
		createdAt: activatedAt, activatedAt: &activatedAt,
	}
	switch allowed := e.allowed.connection.(type) {
	case reconnection:
		_, err = changeConnection(ctx, tx, allowed.id, func(tx *sql.Tx, c *connection) (bool, error) {
			if c.version != allowed.expectedVersion {
				return false, refusedChanged
			}
			if _, err := tx.ExecContext(ctx, `
				UPDATE external_connection_grant SET status = $2, revoked_at = $3 WHERE connection_id = $1 AND status <> $2`,
				c.id, grantRevoked, activatedAt); err != nil {
				return false, err
			}
			g.connectionID, g.revision = c.id, 1
			if len(c.grants) > 0 {
				g.revision = c.grants[len(c.grants)-1].revision + 1
			}
			c.boundCaller = &g.caller
			return true, insertGrant(ctx, tx, g)
		})
	case newConnection:
		g.connectionID, g.revision = uuid.New(), 1
		err = createConnection(ctx, tx, g, allowed.displayName)
	}
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE connection_enrollment SET exchange_claimed_at = $2 WHERE id = $1`, id, activatedAt)
	return err
}

// createConnection creates the Connection of the grant, bound to the grant's account, with the grant as its first.
func createConnection(ctx context.Context, tx *sql.Tx, g grant, displayName string) error {
	bound, err := encodeAccount(g.caller)
	if err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO external_connection (id, display_name, bound_caller, binding_version, version, created_at, updated_at)
		VALUES ($1, $2, $3::jsonb, 0, 1, $4, $4)`, g.connectionID, displayName, bound, g.createdAt); err != nil {
		return err
	}
	if err := insertGrant(ctx, tx, g); err != nil {
		return err
	}
	return notify(ctx, tx)
}

// insertGrant stores a grant active, in a transaction that is never retried.
func insertGrant(ctx context.Context, tx *sql.Tx, g grant) error {
	caller, err := encodeAccount(g.caller)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO external_connection_grant (id, connection_id, revision, original_caller, issuer, client_id, status,
			created_at, activated_at)
		VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)`,
		g.id, g.connectionID, g.revision, caller, g.issuer, g.clientID, g.status, g.createdAt, g.activatedAt)
	return err
}

// ResolveGrant is the account the grant acts as now: its Connection's bound caller. Only an active grant of the issuer
// and client resolves, and only while its Connection is bound to a labeled account.
func (s *Store) ResolveGrant(ctx context.Context, tx *sql.Tx, id uuid.UUID, issuer, clientID string) (auth.Account, error) {
	var status grantStatus
	var grantIssuer, grantClientID string
	var bound []byte
	err := tx.QueryRowContext(
		ctx, `
		SELECT g.status, g.issuer, g.client_id, c.bound_caller
		FROM external_connection_grant g JOIN external_connection c ON c.id = g.connection_id
		WHERE g.id = $1`, id,
	).Scan(&status, &grantIssuer, &grantClientID, &bound)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return auth.Account{}, refusedGrant
	case err != nil:
		return auth.Account{}, err
	case status != grantActive || grantIssuer != issuer || grantClientID != clientID:
		return auth.Account{}, refusedGrant
	case bound == nil:
		return auth.Account{}, refusedUnbound
	}
	account, err := decodeAccount(bound)
	if err != nil {
		return auth.Account{}, err
	}
	if !s.labeled(account) {
		return auth.Account{}, refusedCaller
	}
	return account, nil
}

// RevokeGrant revokes the grant unless it is revoked already; its Connection keeps its bound caller and its other
// grants. An unknown grant is no error.
func (s *Store) RevokeGrant(ctx context.Context, tx *sql.Tx, id uuid.UUID) error {
	var connectionID uuid.UUID
	err := tx.QueryRowContext(ctx, `SELECT connection_id FROM external_connection_grant WHERE id = $1`, id).Scan(&connectionID)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return nil
	case err != nil:
		return err
	}
	_, err = changeConnection(ctx, tx, connectionID, func(tx *sql.Tx, c *connection) (bool, error) {
		for _, g := range c.grants {
			if g.id == id && g.status != grantRevoked {
				_, err := tx.ExecContext(ctx,
					`UPDATE external_connection_grant SET status = $2, revoked_at = $3 WHERE id = $1`, id, grantRevoked, now())
				return true, err
			}
		}
		return false, nil
	})
	return err
}
