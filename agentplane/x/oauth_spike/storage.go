package oauthspike

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"errors"
	"time"

	jose "github.com/go-jose/go-jose/v4"
	"github.com/lib/pq"
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"github.com/zitadel/oidc/v3/pkg/op"
)

const (
	enrollmentTTL   = 15 * time.Minute
	codeTTL         = time.Minute
	accessTokenTTL  = time.Hour
	refreshTokenTTL = 30 * 24 * time.Hour
)

var (
	// ErrInvalidToken is Authenticate's refusal: the access token is unknown, expired, revoked, or bound to another
	// resource.
	ErrInvalidToken = errors.New("invalid access token")
	// ErrNotPending is Approve's and Pending's refusal: no undecided, unexpired enrollment has that handle.
	ErrNotPending = errors.New("no pending enrollment has this handle")
	// errNoSigningKey answers op's requests for a key: this server signs nothing.
	errNoSigningKey = errors.New("this authorization server signs no tokens")
	// errNotServed answers the parts of op's Storage contract that only the endpoints Server replaces or disables use.
	errNotServed = errors.New("not served by this authorization server")
)

// storage implements op.Storage over the spike's tables. On the paths Server keeps, op calls GetClientByClientID,
// AuthRequestByID, AuthRequestByCode and SaveAuthCode; everything else in the contract refuses.
type storage struct {
	db      *sql.DB
	clients *clientRegistry
}

var _ op.Storage = (*storage)(nil)

func (s *storage) GetClientByClientID(ctx context.Context, clientID string) (op.Client, error) {
	return s.clients.client(ctx, clientID)
}

const selectAuthorization = `SELECT id, client_id, redirect_uri, scopes, state, code_challenge, resource,
	COALESCE(service_account, ''), COALESCE(decided_at, 'epoch') FROM oauth_authorization`

func (s *storage) AuthRequestByID(ctx context.Context, id string) (op.AuthRequest, error) {
	return s.scanAuthorization(s.db.QueryRowContext(ctx, selectAuthorization+` WHERE id = $1 AND expires_at > now()`, id))
}

// AuthRequestByCode finds the authorization a live, unexchanged code was issued for.
func (s *storage) AuthRequestByCode(ctx context.Context, code string) (op.AuthRequest, error) {
	return s.scanAuthorization(s.db.QueryRowContext(ctx,
		selectAuthorization+` WHERE code_hash = $1 AND code_expires_at > now() AND exchanged_at IS NULL`, digest(code)))
}

// scanAuthorization returns an untyped nil on error: op tests the AuthRequest interface against nil.
func (s *storage) scanAuthorization(row *sql.Row) (op.AuthRequest, error) {
	var a authorization
	err := row.Scan(&a.id, &a.clientID, &a.redirectURI, pq.Array(&a.scopes), &a.state, &a.codeChallenge, &a.resource,
		&a.serviceAccount, &a.decidedAt)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return nil, oidc.ErrInvalidGrant().WithDescription("unknown, expired or used authorization")
	case err != nil:
		return nil, err
	}
	return &a, nil
}

// SaveAuthCode stores the one code an approved authorization gets; a second callback finds code_hash set.
func (s *storage) SaveAuthCode(ctx context.Context, id string, code string) error {
	result, err := s.db.ExecContext(ctx, `
		UPDATE oauth_authorization SET code_hash = $2, code_expires_at = now() + make_interval(secs => $3)
		WHERE id = $1 AND decided_at IS NOT NULL AND code_hash IS NULL AND expires_at > now()`,
		id, digest(code), codeTTL.Seconds())
	if err != nil {
		return err
	}
	updated, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if updated != 1 {
		return oidc.ErrInvalidRequest().WithDescription("this authorization already issued its code")
	}
	return nil
}

func (s *storage) Health(ctx context.Context) error { return s.db.PingContext(ctx) }

// Enrollment is what the consent page shows before the operator picks a ServiceAccount.
type Enrollment struct {
	ClientID    string
	RedirectURI string
	Resource    string
}

// createAuthorization records a validated authorization request as a pending enrollment and returns the handle the
// consent page addresses it by. Only the handle's digest is stored.
func (s *storage) createAuthorization(ctx context.Context, request *oidc.AuthRequest, resource string) (string, error) {
	handle := rand.Text()
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO oauth_authorization
			(client_id, redirect_uri, scopes, state, code_challenge, resource, consent_handle_hash, expires_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, now() + make_interval(secs => $8))`,
		request.ClientID, request.RedirectURI, pq.Array(request.Scopes), request.State, request.CodeChallenge, resource,
		digest(handle), enrollmentTTL.Seconds())
	return handle, err
}

func (s *storage) pending(ctx context.Context, handle string) (Enrollment, error) {
	var e Enrollment
	err := s.db.QueryRowContext(
		ctx, `
		SELECT client_id, redirect_uri, resource FROM oauth_authorization
		WHERE consent_handle_hash = $1 AND decided_at IS NULL AND expires_at > now()`, digest(handle),
	).Scan(&e.ClientID, &e.RedirectURI, &e.Resource)
	if errors.Is(err, sql.ErrNoRows) {
		return Enrollment{}, ErrNotPending
	}
	return e, err
}

// approve records consent once and returns the authorization's id, which the authorize callback resumes by.
func (s *storage) approve(ctx context.Context, handle, serviceAccount string) (string, error) {
	var id string
	err := s.db.QueryRowContext(
		ctx, `
		UPDATE oauth_authorization SET service_account = $2, decided_at = now()
		WHERE consent_handle_hash = $1 AND decided_at IS NULL AND expires_at > now()
		RETURNING id`, digest(handle), serviceAccount,
	).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrNotPending
	}
	return id, err
}

// exchangeCode claims the authorization once and starts its refresh family. A second exchange of the same consent
// finds exchanged_at set and is refused, whatever code it presents.
func (s *storage) exchangeCode(ctx context.Context, a *authorization, withRefresh bool) (*oidc.AccessTokenResponse, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var familyID string
	err = tx.QueryRowContext(
		ctx, `
		WITH claimed AS (
			UPDATE oauth_authorization SET exchanged_at = now()
			WHERE id = $1 AND exchanged_at IS NULL AND code_expires_at > now()
			RETURNING id
		)
		INSERT INTO oauth_refresh_family (authorization_id) SELECT id FROM claimed RETURNING id`, a.id,
	).Scan(&familyID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, oidc.ErrInvalidGrant().WithDescription("the authorization code was already exchanged")
	}
	if err != nil {
		return nil, err
	}
	response, err := issueTokens(ctx, tx, familyID, a.scopes, withRefresh)
	if err != nil {
		return nil, err
	}
	return response, tx.Commit()
}

// rotateRefreshToken spends a refresh token for a new access and refresh token. A spent token presented again
// revokes its whole family (RFC 9700 section 4.14.2).
func (s *storage) rotateRefreshToken(ctx context.Context, clientID, token string, resources []string) (*oidc.AccessTokenResponse, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var familyID, boundClient, resource string
	var scopes []string
	var rotated, revoked bool
	err = tx.QueryRowContext(
		ctx, `
		SELECT f.id, a.client_id, a.resource, a.scopes, t.rotated_at IS NOT NULL, f.revoked_at IS NOT NULL
		FROM oauth_token t
		JOIN oauth_refresh_family f ON f.id = t.family_id
		JOIN oauth_authorization a ON a.id = f.authorization_id
		WHERE t.token_hash = $1 AND t.kind = 'refresh' AND t.expires_at > now()
		FOR UPDATE OF t, f`, digest(token),
	).Scan(&familyID, &boundClient, &resource, pq.Array(&scopes), &rotated, &revoked)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, oidc.ErrInvalidGrant().WithDescription("unknown or expired refresh token")
	}
	if err != nil {
		return nil, err
	}
	if boundClient != clientID || revoked {
		return nil, oidc.ErrInvalidGrant().WithDescription("refresh token revoked or issued to another client")
	}
	if rotated {
		if _, err := tx.ExecContext(ctx, `UPDATE oauth_refresh_family SET revoked_at = now() WHERE id = $1`, familyID); err != nil {
			return nil, err
		}
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return nil, oidc.ErrInvalidGrant().WithDescription("refresh token reused; its token family is revoked")
	}
	if err := requireResource(resources, resource); err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE oauth_token SET rotated_at = now() WHERE token_hash = $1`, digest(token)); err != nil {
		return nil, err
	}
	response, err := issueTokens(ctx, tx, familyID, scopes, true)
	if err != nil {
		return nil, err
	}
	return response, tx.Commit()
}

// Grant is what an access token admits.
type Grant struct {
	ServiceAccount string
	Scopes         []string
	ExpiresAt      time.Time
}

func (s *storage) authenticate(ctx context.Context, token, resource string) (*Grant, error) {
	var g Grant
	var bound string
	err := s.db.QueryRowContext(
		ctx, `
		SELECT a.service_account, a.scopes, a.resource, t.expires_at
		FROM oauth_token t
		JOIN oauth_refresh_family f ON f.id = t.family_id
		JOIN oauth_authorization a ON a.id = f.authorization_id
		WHERE t.token_hash = $1 AND t.kind = 'access' AND t.expires_at > now() AND f.revoked_at IS NULL`,
		digest(token),
	).Scan(&g.ServiceAccount, pq.Array(&g.Scopes), &bound, &g.ExpiresAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrInvalidToken
	}
	if err != nil {
		return nil, err
	}
	// RFC 8707 audience: a token issued for another resource is not valid here.
	if bound != resource {
		return nil, ErrInvalidToken
	}
	return &g, nil
}

// issueTokens mints opaque random tokens and stores only their digests.
func issueTokens(ctx context.Context, tx *sql.Tx, familyID string, scopes []string, withRefresh bool) (*oidc.AccessTokenResponse, error) {
	response := &oidc.AccessTokenResponse{
		AccessToken: rand.Text(),
		TokenType:   oidc.BearerToken,
		ExpiresIn:   uint64(accessTokenTTL.Seconds()),
		Scope:       scopes,
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO oauth_token (token_hash, family_id, kind, expires_at)
		VALUES ($1, $2, 'access', now() + make_interval(secs => $3))`,
		digest(response.AccessToken), familyID, accessTokenTTL.Seconds()); err != nil {
		return nil, err
	}
	if !withRefresh {
		return response, nil
	}
	response.RefreshToken = rand.Text()
	_, err := tx.ExecContext(ctx, `
		INSERT INTO oauth_token (token_hash, family_id, kind, expires_at)
		VALUES ($1, $2, 'refresh', now() + make_interval(secs => $3))`,
		digest(response.RefreshToken), familyID, refreshTokenTTL.Seconds())
	return response, err
}

func digest(secret string) []byte {
	sum := sha256.Sum256([]byte(secret))
	return sum[:]
}

// authorization is one oauth_authorization row as op's AuthRequest.
type authorization struct {
	id, clientID, redirectURI, state, codeChallenge, resource string
	scopes                                                    []string
	// serviceAccount is empty until the operator approves.
	serviceAccount string
	decidedAt      time.Time
}

func (a *authorization) GetID() string          { return a.id }
func (a *authorization) GetACR() string         { return "" }
func (a *authorization) GetAMR() []string       { return nil }
func (a *authorization) GetAudience() []string  { return []string{a.resource} }
func (a *authorization) GetAuthTime() time.Time { return a.decidedAt }
func (a *authorization) GetClientID() string    { return a.clientID }
func (a *authorization) GetNonce() string       { return "" }
func (a *authorization) GetRedirectURI() string { return a.redirectURI }
func (a *authorization) GetResponseType() oidc.ResponseType {
	return oidc.ResponseTypeCode
}
func (a *authorization) GetResponseMode() oidc.ResponseMode { return oidc.ResponseModeQuery }
func (a *authorization) GetScopes() []string                { return a.scopes }
func (a *authorization) GetState() string                   { return a.state }
func (a *authorization) GetSubject() string                 { return a.serviceAccount }
func (a *authorization) Done() bool                         { return a.serviceAccount != "" }

func (a *authorization) GetCodeChallenge() *oidc.CodeChallenge {
	return &oidc.CodeChallenge{Challenge: a.codeChallenge, Method: oidc.CodeChallengeMethodS256}
}

// The rest of op.Storage serves endpoints Server replaces (CreateAuthRequest, token creation) or leaves disabled
// (revocation, introspection, userinfo, end session, keys, JWT profile).

func (s *storage) CreateAuthRequest(context.Context, *oidc.AuthRequest, string) (op.AuthRequest, error) {
	return nil, errNotServed
}
func (s *storage) DeleteAuthRequest(context.Context, string) error { return errNotServed }
func (s *storage) CreateAccessToken(context.Context, op.TokenRequest) (string, time.Time, error) {
	return "", time.Time{}, errNotServed
}

func (s *storage) CreateAccessAndRefreshTokens(context.Context, op.TokenRequest, string) (string, string, time.Time, error) {
	return "", "", time.Time{}, errNotServed
}

func (s *storage) TokenRequestByRefreshToken(context.Context, string) (op.RefreshTokenRequest, error) {
	return nil, errNotServed
}
func (s *storage) TerminateSession(context.Context, string, string) error { return errNotServed }
func (s *storage) RevokeToken(context.Context, string, string, string) *oidc.Error {
	return oidc.ErrServerError().WithParent(errNotServed)
}

func (s *storage) GetRefreshTokenInfo(context.Context, string, string) (string, string, error) {
	return "", "", op.ErrInvalidRefreshToken
}
func (s *storage) SigningKey(context.Context) (op.SigningKey, error) { return nil, errNoSigningKey }
func (s *storage) SignatureAlgorithms(context.Context) ([]jose.SignatureAlgorithm, error) {
	return nil, errNoSigningKey
}
func (s *storage) KeySet(context.Context) ([]op.Key, error) { return nil, errNoSigningKey }
func (s *storage) AuthorizeClientIDSecret(context.Context, string, string) error {
	return oidc.ErrInvalidClient().WithDescription("clients are public")
}

func (s *storage) SetUserinfoFromScopes(context.Context, *oidc.UserInfo, string, string, []string) error {
	return errNotServed
}

func (s *storage) SetUserinfoFromToken(context.Context, *oidc.UserInfo, string, string, string) error {
	return errNotServed
}

func (s *storage) SetIntrospectionFromToken(context.Context, *oidc.IntrospectionResponse, string, string, string) error {
	return errNotServed
}

func (s *storage) GetPrivateClaimsFromScopes(context.Context, string, string, []string) (map[string]any, error) {
	return nil, errNotServed
}

func (s *storage) GetKeyByIDAndClientID(context.Context, string, string) (*jose.JSONWebKey, error) {
	return nil, errNotServed
}

func (s *storage) ValidateJWTProfileScopes(context.Context, string, []string) ([]string, error) {
	return nil, errNotServed
}
