package connections

import (
	"context"
	"crypto/rand"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
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
	refusedCaller      Refusal = "Identity is missing or no longer current"
	refusedGrant       Refusal = "grant is not authorized"
	refusedUnbound     Refusal = "Connection requires an Identity binding"
	refusedLegacy      Refusal = "legacy ServiceAccount binding requires operator Identity rebind"
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
// second grant. The consent must be live, decided by the approver, and its exact Identity must still be live.
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
	case e.allowed.identity == nil:
		return refusedCaller
	}
	live, err := s.liveIdentity(ctx, *e.allowed.identity)
	if err != nil {
		return err
	}
	if !live {
		return refusedCaller
	}
	activatedAt := now()
	g := grant{
		id: id, identity: e.allowed.identity, issuer: e.issuer, clientID: e.clientID, status: grantActive,
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
			c.boundIdentity = g.identity
			c.identityBindingRequired = false
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

// createConnection creates the Connection of the grant, bound to the grant's Identity, with the grant as its first.
func createConnection(ctx context.Context, tx *sql.Tx, g grant, displayName string) error {
	if g.identity == nil {
		return refusedCaller
	}
	bound, err := encodeIdentity(*g.identity)
	if err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO external_connection (id, display_name, bound_identity, identity_binding_required, binding_version,
			version, created_at, updated_at)
		VALUES ($1, $2, $3::jsonb, false, 0, 1, $4, $4)`, g.connectionID, displayName, bound, g.createdAt); err != nil {
		return err
	}
	if err := insertGrant(ctx, tx, g); err != nil {
		return err
	}
	return notify(ctx, tx)
}

// insertGrant stores a grant active, in a transaction that is never retried.
func insertGrant(ctx context.Context, tx *sql.Tx, g grant) error {
	if g.identity == nil {
		return refusedCaller
	}
	identity, err := encodeIdentity(*g.identity)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO external_connection_grant (id, connection_id, revision, original_identity, issuer, client_id, status,
			created_at, activated_at)
		VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)`,
		g.id, g.connectionID, g.revision, identity, g.issuer, g.clientID, g.status, g.createdAt, g.activatedAt)
	return err
}

// ResolvedGrant is a live OAuth grant and the exact Connection Identity it currently delegates.
type ResolvedGrant struct {
	ConnectionID uuid.UUID
	GrantID      uuid.UUID
	Identity     auth.Identity
}

// ResolveGrant resolves an active OAuth grant to its Connection's live Identity. Legacy ServiceAccount bindings are
// retained for operator review but can never be returned as an effective caller.
func (s *Store) ResolveGrant(ctx context.Context, tx *sql.Tx, id uuid.UUID, issuer, clientID string) (ResolvedGrant, error) {
	var status grantStatus
	var grantIssuer, grantClientID string
	var connectionID uuid.UUID
	var bound []byte
	var identityBindingRequired bool
	err := tx.QueryRowContext(
		ctx, `
		SELECT g.status, g.issuer, g.client_id, g.connection_id, c.bound_identity, c.identity_binding_required
		FROM external_connection_grant g JOIN external_connection c ON c.id = g.connection_id
		WHERE g.id = $1`, id,
	).Scan(&status, &grantIssuer, &grantClientID, &connectionID, &bound, &identityBindingRequired)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return ResolvedGrant{}, refusedGrant
	case err != nil:
		return ResolvedGrant{}, err
	case status != grantActive || grantIssuer != issuer || grantClientID != clientID:
		return ResolvedGrant{}, refusedGrant
	case bound == nil:
		if identityBindingRequired {
			return ResolvedGrant{}, refusedLegacy
		}
		return ResolvedGrant{}, refusedUnbound
	}
	identity, err := decodeIdentity(bound)
	if err != nil {
		return ResolvedGrant{}, err
	}
	live, err := s.liveIdentity(ctx, identity)
	if err != nil {
		return ResolvedGrant{}, err
	}
	if !live {
		return ResolvedGrant{}, refusedCaller
	}
	return ResolvedGrant{ConnectionID: connectionID, GrantID: id, Identity: identity}, nil
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
