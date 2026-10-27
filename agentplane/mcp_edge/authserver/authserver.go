// Package authserver is the OAuth authorization server external MCP clients authorize against before they call /mcp.
//
// An authorization asks an operator's consent through a Connection enrollment, then needs the sign-in of the one
// pinned operator at the upstream OIDC provider. Its code exchange turns the consent into the active grant of a
// Connection, and every token it issues names that grant: a bearer admits a request as the account the grant's
// Connection is bound to at that moment, so revoking, unbinding or rebinding a Connection changes only the requests
// after it.
//
// It reaches Connections and enrollments only through package connections and owns the tables
// oauth_registered_client, oauth_authorization and oauth_token. Its tokens are opaque; only their SHA-256 digests are
// stored.
package authserver

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/oauthex"
	httphelper "github.com/zitadel/oidc/v3/pkg/http" // its package name, http, is net/http's
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"github.com/zitadel/oidc/v3/pkg/op"

	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connections"
	"github.com/agentydragon/ducktape/agentplane/postgres/go"
)

const (
	// scopeActions is the one scope every token carries. A grant's authority is its Connection's bound account, so a
	// requested scope narrows nothing.
	scopeActions = "actions"
	// resourcePath is the protected resource's path under the issuer: the MCP endpoint.
	resourcePath = "/mcp"
)

// Config pins the deployment.
type Config struct {
	// Issuer is the issuer identifier and the base URL of the endpoints: https, or http on a loopback host, with no
	// path, query or fragment.
	Issuer string
	// ConsentURL is the base URL of the app whose page `#/connection-enrollments/<handle>` an operator decides
	// consent on.
	ConsentURL string
	// UpstreamIssuer is the OIDC provider the pinned operator signs in at; New reads its discovery document.
	UpstreamIssuer                         string
	UpstreamClientID, UpstreamClientSecret string
	// UpstreamSubject is the subject of the one upstream account whose sign-in completes an authorization.
	UpstreamSubject string
	// Approver is the operator whose consent an authorization needs.
	Approver connections.Operator
	// DocumentClient fetches Client ID Metadata Documents: NewDocumentClient in production.
	DocumentClient *http.Client
}

// Server is the authorization server. op routes and decodes the authorize, token and revocation requests, resolves
// their client, checks the redirect URI, response type and PKCE method, and renders or redirects their errors; the
// Server does the rest, and serves the metadata, registration and upstream callback endpoints itself.
type Server struct {
	op.UnimplementedServer
	db          *sql.DB
	connections *connections.Store
	issuer      string
	// resource is the one protected resource (RFC 8707) every token is for.
	resource   string
	consentURL string
	approver   connections.Operator
	upstream   *upstream
	// documents fetches client metadata documents and follows no redirect.
	documents *http.Client
	// encoder encodes the authorization responses, codes and errors alike, with RFC 9207's iss.
	encoder issuerEncoder
}

// New returns the server once it has read the upstream provider's discovery document.
func New(ctx context.Context, config Config, db *sql.DB, store *connections.Store) (*Server, error) {
	issuer, err := url.Parse(config.Issuer)
	if err != nil {
		return nil, fmt.Errorf("issuer: %w", err)
	}
	if _, loopback := op.HTTPLoopbackOrLocalhost(config.Issuer); !(issuer.Scheme == "https" || issuer.Scheme == "http" && loopback) ||
		issuer.Host == "" || issuer.Path != "" || issuer.RawQuery != "" || issuer.Fragment != "" || issuer.User != nil {
		return nil, fmt.Errorf("issuer %q must be an https origin, or http on a loopback host", config.Issuer)
	}
	upstream, err := newUpstream(ctx, config, config.Issuer+callbackPath)
	if err != nil {
		return nil, fmt.Errorf("upstream OIDC provider %q: %w", config.UpstreamIssuer, err)
	}
	documents := *config.DocumentClient
	documents.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return &Server{
		db: db, connections: store, issuer: config.Issuer, resource: config.Issuer + resourcePath,
		consentURL: strings.TrimRight(config.ConsentURL, "/"), approver: config.Approver, upstream: upstream,
		documents: &documents, encoder: issuerEncoder{issuer: config.Issuer, encoder: oidc.NewEncoder()},
	}, nil
}

const (
	metadataPath         = "/.well-known/oauth-authorization-server"
	resourceMetadataPath = "/.well-known/oauth-protected-resource" + resourcePath
	authorizePath        = "/authorize"
	tokenPath            = "/token"
	revocationPath       = "/revoke"
	registrationPath     = "/register"
	callbackPath         = "/auth/callback"
)

// ResourceMetadataURL is the protected resource metadata (RFC 9728) the Handler serves for /mcp, which a refused
// bearer's challenge names.
func (s *Server) ResourceMetadataURL() string { return s.issuer + resourceMetadataPath }

// Handler serves the authorization server's endpoints and the protected resource metadata of /mcp, at the paths the
// Python service served them.
func (s *Server) Handler() http.Handler {
	protocol := op.RegisterServer(s, op.Endpoints{
		Authorization: op.NewEndpoint(authorizePath),
		Token:         op.NewEndpoint(tokenPath),
		Revocation:    op.NewEndpoint(revocationPath),
	}, op.WithEncoder(s.encoder), op.WithServerCORSOptions(nil))
	mux := http.NewServeMux()
	mux.Handle(authorizePath, protocol)
	mux.Handle(tokenPath, anyOrigin(protocol))
	mux.Handle(revocationPath, anyOrigin(protocol))
	mux.Handle(metadataPath, anyOrigin(http.HandlerFunc(s.serveMetadata)))
	mux.Handle(resourceMetadataPath, auth.ProtectedResourceMetadataHandler(&oauthex.ProtectedResourceMetadata{
		Resource:               s.resource,
		AuthorizationServers:   []string{s.issuer},
		ScopesSupported:        []string{scopeActions},
		BearerMethodsSupported: []string{"header"},
	}))
	mux.Handle(registrationPath, anyOrigin(http.HandlerFunc(s.register)))
	mux.HandleFunc("GET "+callbackPath, s.callback)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mux.ServeHTTP(retryLater{w}, r)
	})
}

// authServerMetadata omits jwks_uri when the issuer has no JWK Set endpoint. RFC 8414 makes that
// member optional, while the SDK's AuthServerMeta always serializes it.
type authServerMetadata struct {
	oauthex.AuthServerMeta
	JWKSURI *string `json:"jwks_uri,omitempty"`
}

// serveMetadata answers RFC 8414 authorization server metadata. op's own discovery document is OpenID Connect's,
// which has no registration endpoint, CIMD or RFC 9207 field.
func (s *Server) serveMetadata(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	metadata := authServerMetadata{AuthServerMeta: oauthex.AuthServerMeta{
		Issuer:                                     s.issuer,
		AuthorizationEndpoint:                      s.issuer + authorizePath,
		TokenEndpoint:                              s.issuer + tokenPath,
		RegistrationEndpoint:                       s.issuer + registrationPath,
		RevocationEndpoint:                         s.issuer + revocationPath,
		ScopesSupported:                            []string{scopeActions},
		ResponseTypesSupported:                     []string{string(oidc.ResponseTypeCode)},
		ResponseModesSupported:                     []string{string(oidc.ResponseModeQuery)},
		GrantTypesSupported:                        []string{string(oidc.GrantTypeCode), string(oidc.GrantTypeRefreshToken)},
		TokenEndpointAuthMethodsSupported:          []string{string(oidc.AuthMethodNone)},
		RevocationEndpointAuthMethodsSupported:     []string{string(oidc.AuthMethodNone)},
		CodeChallengeMethodsSupported:              []string{string(oidc.CodeChallengeMethodS256)},
		ClientIDMetadataDocumentSupported:          true,
		AuthorizationResponseIssParameterSupported: true,
	}}
	_ = json.NewEncoder(w).Encode(metadata)
}

// anyOrigin lets a browser-based client call the endpoint from any origin. No endpoint reads a cookie, so a
// cross-origin call carries no ambient authority.
func anyOrigin(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		if r.Method == http.MethodOptions {
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type, MCP-Protocol-Version")
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// retryLater adds Retry-After to every 503 the server answers: each is an outage of the database or the upstream
// provider, which a later retry may get past.
type retryLater struct{ http.ResponseWriter }

func (w retryLater) WriteHeader(status int) {
	if status == http.StatusServiceUnavailable {
		w.Header().Set("Retry-After", "60")
	}
	w.ResponseWriter.WriteHeader(status)
}

// unavailableDescription is what a client hears of an outage; its cause is logged.
const unavailableDescription = "Connection authorization is temporarily unavailable."

// unavailable answers an outage of the database or the upstream provider at the token, revocation and registration
// endpoints: 503, which the client retries.
func unavailable(err error) error {
	return op.NewStatusError(oidc.ErrServerError().WithDescription(unavailableDescription).WithParent(err),
		http.StatusServiceUnavailable)
}

// issuerEncoder adds RFC 9207's iss to the authorization responses it encodes: op's response types have no iss.
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

// digest is how a code, a token or a state is stored: the SHA-256 hex of its value.
func digest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// requireResource checks a request's RFC 8707 resource parameters: a client may omit resource, and may name only
// the one resource this server issues tokens for.
func (s *Server) requireResource(requested []string) error {
	if len(requested) == 0 || len(requested) == 1 && requested[0] == s.resource {
		return nil
	}
	return oidc.ErrInvalidTarget().WithDescription("resource must be %s", s.resource)
}

func (s *Server) transaction(ctx context.Context, f func(*sql.Tx) error) error {
	return postgres.Transaction(ctx, s.db, sql.TxOptions{}, f)
}
