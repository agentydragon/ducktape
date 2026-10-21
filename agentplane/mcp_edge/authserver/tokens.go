package authserver

import (
	"context"
	"crypto/rand"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"github.com/zitadel/oidc/v3/pkg/op"

	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connections"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/mcpserver"
)

const (
	// accessTokenLifetime is an access token's longest life; the operator's upstream session lapsing ends it sooner.
	accessTokenLifetime  = time.Hour
	refreshTokenLifetime = 30 * 24 * time.Hour
)

// What a refused token request is told: fixed sentences naming the check, never a value.
const (
	codeInvalid    = "The authorization code is unknown, expired, or was issued to another client; authorize a new connection."
	codeReplayed   = "The authorization code was already used; the tokens it issued are revoked."
	redirectDiffer = "redirect_uri differs from the authorization request's."
	grantInvalid   = "The Connection authorization grant is invalid; authorize a new connection."
	refreshReused  = "The refresh token was already used; the tokens of its authorization are revoked."
	upstreamLapsed = "The operator's upstream sign-in has lapsed; authorize a new connection."
)

// VerifyClient admits a public client: one that sends no secret or assertion.
func (s *Server) VerifyClient(ctx context.Context, r *op.Request[op.ClientCredentials]) (op.Client, error) {
	if r.Data.ClientSecret != "" || r.Data.ClientAssertion != "" {
		return nil, oidc.ErrInvalidClient().WithDescription("clients of this server are public: they send no secret or assertion")
	}
	c, err := s.client(ctx, r.Data.ClientID)
	if err != nil {
		return nil, err
	}
	return c, nil
}

// CodeExchange turns the authorization's consent into the active grant of its Connection and issues the grant's
// tokens, in one transaction. A code exchanged a second time revokes the grant, and so the tokens the first exchange
// issued.
func (s *Server) CodeExchange(ctx context.Context, r *op.ClientRequest[oidc.AccessTokenRequest]) (*op.Response, error) {
	if err := s.requireResource(r.Form["resource"]); err != nil {
		return nil, err
	}
	var response *op.Response
	// replayed is the refusal of a replay, which commits the revocation.
	var replayed error
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var (
			id                                   uuid.UUID
			clientID, redirectURI, codeChallenge string
			signedInAt, upstreamExpiresAt        time.Time
			upstreamRefreshToken                 *string
			exchangedAt                          *time.Time
		)
		err := tx.QueryRowContext(
			ctx, `
			SELECT id, client_id, redirect_uri, code_challenge, signed_in_at, upstream_expires_at, upstream_refresh_token,
				exchanged_at
			FROM oauth_authorization WHERE code_hash = $1 FOR UPDATE`, digest(r.Data.Code),
		).Scan(&id, &clientID, &redirectURI, &codeChallenge, &signedInAt, &upstreamExpiresAt, &upstreamRefreshToken, &exchangedAt)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			return oidc.ErrInvalidGrant().WithDescription(codeInvalid)
		case err != nil:
			return err
		case clientID != r.Client.GetID():
			return oidc.ErrInvalidGrant().WithDescription(codeInvalid)
		case exchangedAt != nil:
			replayed = oidc.ErrInvalidGrant().WithDescription(codeReplayed)
			return s.connections.RevokeGrant(ctx, tx, id)
		case time.Since(signedInAt) > codeLifetime:
			return oidc.ErrInvalidGrant().WithDescription(codeInvalid)
		case r.Data.RedirectURI != redirectURI:
			return oidc.ErrInvalidGrant().WithDescription(redirectDiffer)
		}
		challenge := &oidc.CodeChallenge{Challenge: codeChallenge, Method: oidc.CodeChallengeMethodS256}
		if err := op.AuthorizeCodeChallenge(r.Data.CodeVerifier, challenge); err != nil {
			return err
		}
		if err := s.connections.Activate(ctx, tx, id, s.approver); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE oauth_authorization SET exchanged_at = $2 WHERE id = $1`, id, time.Now()); err != nil {
			return err
		}
		refreshable := upstreamRefreshToken != nil && op.ValidateGrantType(r.Client, oidc.GrantTypeRefreshToken)
		response, err = issue(ctx, tx, id, upstreamExpiresAt, refreshable)
		return err
	})
	switch {
	case err != nil:
		return nil, tokenError(err)
	case replayed != nil:
		return nil, replayed
	}
	return response, nil
}

// RefreshToken renews the operator's upstream session, which must still be the pinned operator's, and rotates the
// refresh token. A refresh token spent a second time revokes the grant.
func (s *Server) RefreshToken(ctx context.Context, r *op.ClientRequest[oidc.RefreshTokenRequest]) (*op.Response, error) {
	if err := s.requireResource(r.Form["resource"]); err != nil {
		return nil, err
	}
	var response *op.Response
	// reused is the refusal of a reuse, which commits the revocation.
	var reused error
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var (
			id                   uuid.UUID
			clientID             string
			expiresAt            time.Time
			rotatedAt            *time.Time
			upstreamRefreshToken *string
		)
		err := tx.QueryRowContext(
			ctx, `
			SELECT a.id, a.client_id, t.expires_at, t.rotated_at, a.upstream_refresh_token
			FROM oauth_token t JOIN oauth_authorization a ON a.id = t.authorization_id
			WHERE t.token_hash = $1 AND t.kind = 'refresh'
			FOR UPDATE`, digest(r.Data.RefreshToken),
		).Scan(&id, &clientID, &expiresAt, &rotatedAt, &upstreamRefreshToken)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			return oidc.ErrInvalidGrant().WithDescription(grantInvalid)
		case err != nil:
			return err
		case clientID != r.Client.GetID():
			return oidc.ErrInvalidGrant().WithDescription(grantInvalid)
		case rotatedAt != nil:
			reused = oidc.ErrInvalidGrant().WithDescription(refreshReused)
			return s.connections.RevokeGrant(ctx, tx, id)
		case !expiresAt.After(time.Now()) || upstreamRefreshToken == nil:
			return oidc.ErrInvalidGrant().WithDescription(grantInvalid)
		}
		if _, err := s.connections.ResolveGrant(ctx, tx, id, s.issuer, clientID); err != nil {
			return err
		}
		signedIn, err := s.upstream.renew(ctx, *upstreamRefreshToken)
		switch {
		case errors.Is(err, errUpstreamDown):
			return err
		case errors.Is(err, errNotOperator):
			return oidc.ErrInvalidGrant().WithDescription(notOperator)
		case err != nil:
			return oidc.ErrInvalidGrant().WithDescription(upstreamUnverified).WithParent(err)
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE oauth_authorization SET upstream_expires_at = $2, upstream_refresh_token = $3 WHERE id = $1`,
			id, signedIn.expiresAt, signedIn.refreshToken); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx,
			`UPDATE oauth_token SET rotated_at = $2 WHERE token_hash = $1`, digest(r.Data.RefreshToken), time.Now()); err != nil {
			return err
		}
		response, err = issue(ctx, tx, id, signedIn.expiresAt, signedIn.refreshToken != nil)
		return err
	})
	switch {
	case err != nil:
		return nil, tokenError(err)
	case reused != nil:
		return nil, reused
	}
	return response, nil
}

// issue mints the authorization's access token, and a refresh token when refreshable. The access token lapses with
// the operator's upstream session at the latest.
func issue(ctx context.Context, tx *sql.Tx, id uuid.UUID, upstreamExpiresAt time.Time, refreshable bool) (*op.Response, error) {
	now := time.Now()
	expiresAt := now.Add(accessTokenLifetime)
	if upstreamExpiresAt.Before(expiresAt) {
		expiresAt = upstreamExpiresAt
	}
	if !expiresAt.After(now) {
		return nil, oidc.ErrInvalidGrant().WithDescription(upstreamLapsed)
	}
	tokens := &oidc.AccessTokenResponse{
		AccessToken: rand.Text(), TokenType: oidc.BearerToken, ExpiresIn: uint64(expiresAt.Sub(now) / time.Second),
		Scope: oidc.SpaceDelimitedArray{scopeActions},
	}
	store := func(token, kind string, expiresAt time.Time) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO oauth_token (token_hash, authorization_id, kind, expires_at) VALUES ($1, $2, $3, $4)`,
			digest(token), id, kind, expiresAt)
		return err
	}
	if err := store(tokens.AccessToken, "access", expiresAt); err != nil {
		return nil, err
	}
	if refreshable {
		tokens.RefreshToken = rand.Text()
		if err := store(tokens.RefreshToken, "refresh", now.Add(refreshTokenLifetime)); err != nil {
			return nil, err
		}
	}
	response := op.NewResponse(tokens)
	response.Header.Set("Cache-Control", "no-store")
	response.Header.Set("Pragma", "no-cache")
	return response, nil
}

// Revocation (RFC 7009) revokes the grant of a token the client holds. A token that is unknown, or another client's,
// is answered the same and revokes nothing.
func (s *Server) Revocation(ctx context.Context, r *op.ClientRequest[oidc.RevocationRequest]) (*op.Response, error) {
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var id uuid.UUID
		var clientID string
		err := tx.QueryRowContext(
			ctx, `
			SELECT a.id, a.client_id FROM oauth_token t JOIN oauth_authorization a ON a.id = t.authorization_id
			WHERE t.token_hash = $1`, digest(r.Data.Token),
		).Scan(&id, &clientID)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			return nil
		case err != nil:
			return err
		case clientID != r.Client.GetID():
			return nil
		}
		return s.connections.RevokeGrant(ctx, tx, id)
	})
	if err != nil {
		return nil, unavailable(err)
	}
	response := op.NewResponse(struct{}{})
	response.Header.Set("Cache-Control", "no-store")
	response.Header.Set("Pragma", "no-cache")
	return response, nil
}

// tokenError is the token endpoint's answer to err: an OAuth error as it is, a Connections refusal as invalid_grant,
// and anything else, a fault of the database or the upstream provider, as an outage.
func tokenError(err error) error {
	var oauthError *oidc.Error
	var refused connections.Refusal
	switch {
	case errors.As(err, &oauthError):
		return err
	case errors.As(err, &refused):
		return oidc.ErrInvalidGrant().WithDescription("%s", refused)
	default:
		return unavailable(err)
	}
}

// VerifyBearer is /mcp's auth.TokenVerifier. It admits an unexpired access token this server issued as the account
// its grant's Connection is bound to now, which TokenInfo.UserID holds as `namespace/name`. A fault of the database
// is wrapped in mcpserver.ErrAuthorityUnavailable.
func (s *Server) VerifyBearer(ctx context.Context, token string, _ *http.Request) (*auth.TokenInfo, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, fmt.Errorf("%w: %w", mcpserver.ErrAuthorityUnavailable, err)
	}
	defer tx.Rollback()
	var id uuid.UUID
	var clientID string
	var expiresAt time.Time
	err = tx.QueryRowContext(
		ctx, `
		SELECT a.id, a.client_id, t.expires_at FROM oauth_token t JOIN oauth_authorization a ON a.id = t.authorization_id
		WHERE t.token_hash = $1 AND t.kind = 'access'`, digest(token),
	).Scan(&id, &clientID, &expiresAt)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return nil, auth.ErrInvalidToken
	case err != nil:
		return nil, fmt.Errorf("%w: %w", mcpserver.ErrAuthorityUnavailable, err)
	case !expiresAt.After(time.Now()):
		return nil, auth.ErrInvalidToken
	}
	account, err := s.connections.ResolveGrant(ctx, tx, id, s.issuer, clientID)
	var refused connections.Refusal
	switch {
	case errors.As(err, &refused):
		return nil, fmt.Errorf("%w: %w", auth.ErrInvalidToken, err)
	case err != nil:
		return nil, fmt.Errorf("%w: %w", mcpserver.ErrAuthorityUnavailable, err)
	}
	return &auth.TokenInfo{
		UserID: account.Namespace + "/" + account.Name, Expiration: expiresAt, Scopes: []string{scopeActions},
	}, nil
}
