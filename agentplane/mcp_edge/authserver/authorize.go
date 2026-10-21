package authserver

import (
	"context"
	"crypto/rand"
	"database/sql"
	"errors"
	"log/slog"
	"net/http"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"github.com/zitadel/oidc/v3/pkg/op"
	"golang.org/x/oauth2"

	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connections"
)

// codeLifetime is how long a code may wait for its exchange after the operator's upstream sign-in.
const codeLifetime = 5 * time.Minute

// VerifyAuthRequest resolves the client of an authorization request. op then checks the redirect URI against it,
// and from there on answers a refusal with a redirect to the client.
func (s *Server) VerifyAuthRequest(ctx context.Context, r *op.Request[oidc.AuthRequest]) (*op.ClientRequest[oidc.AuthRequest], error) {
	if r.Data.RequestParam != "" {
		return nil, oidc.ErrRequestNotSupported()
	}
	c, err := s.client(ctx, r.Data.ClientID)
	if err != nil {
		return nil, err
	}
	// OAuth 2.1 makes scope optional, and MCP clients omit it when nothing advertises one, but op refuses an
	// authorization request without one.
	if len(r.Data.Scopes) == 0 {
		r.Data.Scopes = oidc.SpaceDelimitedArray{scopeActions}
	}
	return &op.ClientRequest[oidc.AuthRequest]{Request: r, Client: c}, nil
}

// Authorize records the authorization and its consent enrollment, and sends the browser to an operator's consent.
// The consent, once allowed, sends it on to the upstream sign-in.
func (s *Server) Authorize(ctx context.Context, r *op.ClientRequest[oidc.AuthRequest]) (*op.Redirect, error) {
	request := r.Data
	switch {
	case slices.Contains(request.Prompt, oidc.PromptNone):
		return nil, oidc.ErrInteractionRequired().WithDescription("an operator must consent to every authorization")
	case request.ResponseMode != "" && request.ResponseMode != oidc.ResponseModeQuery:
		return nil, oidc.ErrInvalidRequest().WithDescription("only the query response mode is supported")
	case request.CodeChallenge == "" || request.CodeChallengeMethod != oidc.CodeChallengeMethodS256:
		return nil, oidc.ErrInvalidRequest().WithDescription("an S256 PKCE code challenge is required")
	}
	if err := s.requireResource(r.Form["resource"]); err != nil {
		return nil, err
	}
	id := uuid.New()
	upstreamState, upstreamVerifier := rand.Text(), oauth2.GenerateVerifier()
	var state *string
	if request.State != "" {
		state = &request.State
	}
	var handle string
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		var expiresAt time.Time
		var err error
		handle, expiresAt, err = s.connections.CreateEnrollment(ctx, tx, connections.NewEnrollment{
			ID: id, Issuer: s.issuer, ClientID: request.ClientID, ClientName: r.Client.(*client).name(),
			RedirectURI: request.RedirectURI, CodeChallenge: request.CodeChallenge,
			UpstreamURL: s.upstream.authorizeURL(upstreamState, upstreamVerifier),
		})
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `
			INSERT INTO oauth_authorization (id, client_id, redirect_uri, code_challenge, state, expires_at,
				upstream_state_hash, upstream_verifier)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
			id, request.ClientID, request.RedirectURI, request.CodeChallenge, state, expiresAt, digest(upstreamState),
			upstreamVerifier)
		return err
	})
	var refused connections.Refusal
	switch {
	case errors.As(err, &refused):
		return nil, oidc.ErrInvalidRequest().WithDescription("This authorization interaction cannot be reused.").WithParent(err)
	case err != nil:
		return nil, temporarilyUnavailable(err)
	}
	return op.NewRedirect(s.consentURL + "/#/connection-enrollments/" + handle), nil
}

// temporarilyUnavailable is an outage answered to the client's redirect URI (RFC 6749 section 4.1.2.1).
func temporarilyUnavailable(err error) *oidc.Error {
	return (&oidc.Error{ErrorType: "temporarily_unavailable", Description: unavailableDescription}).WithParent(err)
}

const (
	notOperator         = "The upstream account that signed in is not this service's operator."
	upstreamUnverified  = "The upstream sign-in could not be verified; authorize a new connection."
	upstreamRefused     = "The upstream sign-in was refused; authorize a new connection."
	authorizationExpiry = "The authorization expired; authorize a new connection."
)

// callback is where the upstream provider sends the browser back. A sign-in of the pinned operator earns the
// authorization its one code, which the browser carries to the client's redirect URI; anything else sends the client
// an error there.
func (s *Server) callback(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	query := r.URL.Query()
	var (
		id                            uuid.UUID
		redirectURI, upstreamVerifier string
		state                         *string
		expiresAt                     time.Time
	)
	err := s.db.QueryRowContext(
		ctx, `
		SELECT id, redirect_uri, state, expires_at, upstream_verifier FROM oauth_authorization
		WHERE upstream_state_hash = $1 AND code_hash IS NULL`, digest(query.Get("state")),
	).Scan(&id, &redirectURI, &state, &expiresAt, &upstreamVerifier)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		http.Error(w, "The sign-in matches no authorization in progress.", http.StatusBadRequest)
		return
	case err != nil:
		slog.WarnContext(ctx, "reading an authorization failed", "error", err)
		http.Error(w, unavailableDescription, http.StatusServiceUnavailable)
		return
	}
	refuse := func(refusal *oidc.Error) {
		if state != nil {
			refusal.State = *state
		}
		slog.WarnContext(ctx, "authorization refused at the upstream callback", "authorization", id, "error", refusal)
		s.redirectToClient(w, r, redirectURI, refusal)
	}
	if !expiresAt.After(time.Now()) {
		refuse(oidc.ErrAccessDenied().WithDescription(authorizationExpiry))
		return
	}
	if query.Has("error") {
		refuse(oidc.ErrAccessDenied().WithDescription(upstreamRefused))
		return
	}
	signedIn, err := s.upstream.signIn(ctx, query.Get("code"), upstreamVerifier)
	switch {
	case errors.Is(err, errUpstreamDown):
		refuse(temporarilyUnavailable(err))
		return
	case errors.Is(err, errNotOperator):
		refuse(oidc.ErrAccessDenied().WithDescription(notOperator))
		return
	case err != nil:
		refuse(oidc.ErrAccessDenied().WithDescription(upstreamUnverified).WithParent(err))
		return
	}
	code := rand.Text()
	result, err := s.db.ExecContext(ctx, `
		UPDATE oauth_authorization SET code_hash = $2, signed_in_at = $3, upstream_expires_at = $4,
			upstream_refresh_token = $5
		WHERE id = $1 AND code_hash IS NULL`, id, digest(code), time.Now(), signedIn.expiresAt, signedIn.refreshToken)
	if err != nil {
		refuse(temporarilyUnavailable(err))
		return
	}
	if issued, err := result.RowsAffected(); err != nil || issued != 1 {
		http.Error(w, "The sign-in matches no authorization in progress.", http.StatusBadRequest)
		return
	}
	response := &op.CodeResponseType{Code: code}
	if state != nil {
		response.State = *state
	}
	s.redirectToClient(w, r, redirectURI, response)
}

// redirectToClient sends the browser to the client's redirect URI with the authorization response, and iss.
func (s *Server) redirectToClient(w http.ResponseWriter, r *http.Request, redirectURI string, response any) {
	location, err := op.AuthResponseURL(redirectURI, oidc.ResponseTypeCode, oidc.ResponseModeQuery, response, s.encoder)
	if err != nil {
		op.WriteError(w, r, err, nil)
		return
	}
	http.Redirect(w, r, location, http.StatusFound)
}
