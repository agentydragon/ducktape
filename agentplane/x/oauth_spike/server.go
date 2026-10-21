package oauthspike

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"

	"github.com/modelcontextprotocol/go-sdk/oauthex"
	httphelper "github.com/zitadel/oidc/v3/pkg/http" // its package name, http, collides with net/http
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"github.com/zitadel/oidc/v3/pkg/op"
)

const (
	authorizationPath = "authorize"
	tokenPath         = "token"
)

// Config pins the authorization server's deployment.
type Config struct {
	// Issuer is the issuer identifier and the base URL of the endpoints.
	Issuer string
	// Resource is the one protected resource (RFC 8707) tokens are issued for: the MCP endpoint's URL.
	Resource string
	// ConsentURL is the integration app's consent page; the enrollment handle is appended to it.
	ConsentURL string
	// AllowInsecureIssuer permits an http issuer, for tests on loopback.
	AllowInsecureIssuer bool
}

// Server is op's LegacyServer with the endpoints a plain OAuth 2.1 server needs replaced: authorize stores a
// durable enrollment and sends the browser to the app's consent page, the code and refresh grants mint opaque tokens
// and no id_token, and discovery is RFC 8414 with CIMD and RFC 9207 advertised. op still routes and decodes requests,
// resolves and authenticates the client, validates redirect_uri, response_type, scopes and the PKCE method, verifies
// the code_verifier, and renders and redirects errors.
type Server struct {
	*op.LegacyServer
	storage *storage
	config  Config
	// encoder encodes every authorization response, success or error, with RFC 9207's iss.
	encoder issuerEncoder
}

// New returns the server and its HTTP handler. metadataFetcher fetches client metadata documents; its redirects are
// not followed.
func New(config Config, db *sql.DB, metadataFetcher *http.Client) (*Server, http.Handler, error) {
	fetch := *metadataFetcher
	fetch.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	store := &storage{db: db, clients: &clientRegistry{db: db, fetch: &fetch}}
	var options []op.Option
	if config.AllowInsecureIssuer {
		options = append(options, op.WithAllowInsecure())
	}
	// op encrypts its own codes and bearer tokens under this key. The paths Server keeps mint neither, so a key no
	// other replica shares turns an accidental use into a refusal instead of a forgeable token.
	var key [32]byte
	rand.Read(key[:])
	provider, err := op.NewProvider(&op.Config{CryptoKey: key}, store, op.StaticIssuer(config.Issuer), options...)
	if err != nil {
		return nil, nil, err
	}
	server := &Server{
		// Nil endpoints are not routed: no userinfo, introspection, revocation, end session or JWKS.
		LegacyServer: op.NewLegacyServer(provider, op.Endpoints{
			Authorization: op.NewEndpoint(authorizationPath),
			Token:         op.NewEndpoint(tokenPath),
		}),
		storage: store,
		config:  config,
		encoder: issuerEncoder{issuer: config.Issuer, encoder: oidc.NewEncoder()},
	}
	mux := http.NewServeMux()
	mux.Handle("/", op.RegisterLegacyServer(server, server.authorizeCallback, op.WithEncoder(server.encoder)))
	// op's router takes no routes ahead of the middleware RegisterLegacyServer installs, so the RFC 8414 path is
	// mounted beside it.
	mux.HandleFunc("GET /.well-known/oauth-authorization-server", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(server.metadata())
	})
	return server, mux, nil
}

// metadata is RFC 8414 authorization server metadata. op's own discovery type is OpenID Connect Discovery, which has
// no authorization_response_iss_parameter_supported.
func (s *Server) metadata() *oauthex.AuthServerMeta {
	return &oauthex.AuthServerMeta{
		Issuer:                                     s.config.Issuer,
		AuthorizationEndpoint:                      s.Endpoints().Authorization.Absolute(s.config.Issuer),
		TokenEndpoint:                              s.Endpoints().Token.Absolute(s.config.Issuer),
		ResponseTypesSupported:                     []string{string(oidc.ResponseTypeCode)},
		GrantTypesSupported:                        []string{string(oidc.GrantTypeCode), string(oidc.GrantTypeRefreshToken)},
		CodeChallengeMethodsSupported:              []string{string(oidc.CodeChallengeMethodS256)},
		TokenEndpointAuthMethodsSupported:          []string{string(oidc.AuthMethodNone)},
		ScopesSupported:                            []string{scopeActions},
		ClientIDMetadataDocumentSupported:          true,
		AuthorizationResponseIssParameterSupported: true,
	}
}

// Discovery answers op's /.well-known/openid-configuration with the same RFC 8414 document.
func (s *Server) Discovery(context.Context, *op.Request[struct{}]) (*op.Response, error) {
	return op.NewResponse(s.metadata()), nil
}

// VerifyAuthRequest gives a request without scope this server's scope: OAuth 2.1 makes scope optional and MCP clients
// omit it when nothing advertises one, but op refuses an empty scope as an OpenID Connect request.
func (s *Server) VerifyAuthRequest(ctx context.Context, r *op.Request[oidc.AuthRequest]) (*op.ClientRequest[oidc.AuthRequest], error) {
	if len(r.Data.Scopes) == 0 {
		r.Data.Scopes = oidc.SpaceDelimitedArray{scopeActions}
	}
	return s.LegacyServer.VerifyAuthRequest(ctx, r)
}

// Authorize runs once op has validated the client, redirect_uri, scopes, response_type and the PKCE method. It
// requires S256, binds the RFC 8707 resource, and stores the enrollment before sending the browser to consent.
func (s *Server) Authorize(ctx context.Context, r *op.ClientRequest[oidc.AuthRequest]) (*op.Redirect, error) {
	if r.Data.RequestParam != "" {
		return nil, oidc.ErrRequestNotSupported()
	}
	if r.Data.CodeChallenge == "" || r.Data.CodeChallengeMethod != oidc.CodeChallengeMethodS256 {
		return nil, oidc.ErrInvalidRequest().WithDescription("S256 PKCE is required")
	}
	if err := requireResource(r.Form["resource"], s.config.Resource); err != nil {
		return nil, err
	}
	handle, err := s.storage.createAuthorization(ctx, r.Data, s.config.Resource)
	if err != nil {
		return nil, err
	}
	return op.NewRedirect(s.config.ConsentURL + url.PathEscape(handle)), nil
}

// Pending reads an enrollment back for the consent page.
func (s *Server) Pending(ctx context.Context, handle string) (Enrollment, error) {
	return s.storage.pending(ctx, handle)
}

// Approve records the operator's consent and returns the URL the browser resumes the authorization at. In the real
// flow the browser first signs in upstream as the pinned operator; that leg ends at the same URL.
func (s *Server) Approve(ctx context.Context, handle, serviceAccount string) (string, error) {
	id, err := s.storage.approve(ctx, handle, serviceAccount)
	if err != nil {
		return "", err
	}
	return s.AuthCallbackURL()(op.ContextWithIssuer(ctx, s.config.Issuer), id), nil
}

// issuerEncoder adds RFC 9207's iss to the authorization responses op encodes, its error redirects included: op's
// response types have no iss field.
type issuerEncoder struct {
	issuer  string
	encoder httphelper.Encoder
}

func (e issuerEncoder) Encode(src any, dst map[string][]string) error {
	if err := e.encoder.Encode(src, dst); err != nil {
		return err
	}
	dst["iss"] = []string{e.issuer}
	return nil
}

// authorizeCallback issues the approved authorization's one code to the client's redirect_uri. op's
// AuthorizeCallback would encode the code itself, without iss.
func (s *Server) authorizeCallback(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	authReq, err := s.storage.AuthRequestByID(ctx, r.URL.Query().Get("id"))
	if err != nil {
		op.WriteError(w, r, err, nil)
		return
	}
	location, err := s.issueCode(ctx, authReq)
	if err != nil {
		redirect, err := op.TryErrorRedirect(ctx, authReq, err, s.encoder, nil)
		if err != nil {
			op.WriteError(w, r, err, nil)
			return
		}
		location = redirect.URL
	}
	http.Redirect(w, r, location, http.StatusFound)
}

func (s *Server) issueCode(ctx context.Context, authReq op.AuthRequest) (string, error) {
	if !authReq.Done() {
		return "", oidc.ErrInteractionRequired().WithDescription("consent is pending")
	}
	code := rand.Text()
	if err := s.storage.SaveAuthCode(ctx, authReq.GetID(), code); err != nil {
		return "", err
	}
	return op.AuthResponseURL(authReq.GetRedirectURI(), authReq.GetResponseType(), authReq.GetResponseMode(),
		&op.CodeResponseType{Code: code, State: authReq.GetState()}, s.encoder)
}

// CodeExchange keeps op's code lookup and PKCE verification and replaces token creation: op's CreateTokenResponse
// signs an id_token for every code exchange, openid or not.
func (s *Server) CodeExchange(ctx context.Context, r *op.ClientRequest[oidc.AccessTokenRequest]) (*op.Response, error) {
	authReq, err := op.AuthRequestByCode(ctx, s.storage, r.Data.Code)
	if err != nil {
		return nil, err
	}
	if err := op.AuthorizeCodeChallenge(r.Data.CodeVerifier, authReq.GetCodeChallenge()); err != nil {
		return nil, err
	}
	if r.Client.GetID() != authReq.GetClientID() {
		return nil, oidc.ErrInvalidGrant()
	}
	if r.Data.RedirectURI != authReq.GetRedirectURI() {
		return nil, oidc.ErrInvalidGrant().WithDescription("redirect_uri does not correspond")
	}
	a, ok := authReq.(*authorization)
	if !ok {
		return nil, errors.New("storage returned a foreign auth request")
	}
	if err := requireResource(r.Form["resource"], a.resource); err != nil {
		return nil, err
	}
	response, err := s.storage.exchangeCode(ctx, a, op.ValidateGrantType(r.Client, oidc.GrantTypeRefreshToken))
	if err != nil {
		return nil, err
	}
	return op.NewResponse(response), nil
}

// RefreshToken rotates the refresh token in one transaction; op's path would split lookup and creation across
// Storage calls and sign an id_token.
func (s *Server) RefreshToken(ctx context.Context, r *op.ClientRequest[oidc.RefreshTokenRequest]) (*op.Response, error) {
	if r.Data.RefreshToken == "" {
		return nil, oidc.ErrInvalidRequest().WithDescription("refresh_token missing")
	}
	response, err := s.storage.rotateRefreshToken(ctx, r.Client.GetID(), r.Data.RefreshToken, r.Form["resource"])
	if err != nil {
		return nil, err
	}
	return op.NewResponse(response), nil
}

// Authenticate resolves an access token for the resource server at resource.
func (s *Server) Authenticate(ctx context.Context, token, resource string) (*Grant, error) {
	return s.storage.authenticate(ctx, token, resource)
}

// requireResource checks a request's RFC 8707 resource parameters against the resource the grant is bound to. A
// client may omit resource; it may not name another resource, or several.
func requireResource(requested []string, bound string) error {
	if len(requested) == 0 || (len(requested) == 1 && requested[0] == bound) {
		return nil
	}
	return oidc.ErrInvalidTarget().WithDescription("resource must be %s", bound)
}
