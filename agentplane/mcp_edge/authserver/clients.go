package authserver

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"time"

	"code.dny.dev/ssrf"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/modelcontextprotocol/go-sdk/oauthex"
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"github.com/zitadel/oidc/v3/pkg/op"
)

const (
	// maxDocumentBytes bounds a client metadata document, as the CIMD draft suggests, and a registration request.
	maxDocumentBytes = 5 << 10
	// documentTimeout bounds a fetch of a client metadata document.
	documentTimeout = 10 * time.Second
)

// NewDocumentClient fetches client metadata documents from public addresses only: a client_id URL is the client's to
// choose, so its dial is refused to a loopback, private, link-local or other special-purpose address, as the egress
// proxy refuses one.
func NewDocumentClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = (&net.Dialer{Control: ssrf.New(ssrf.WithAnyPort()).Safe}).DialContext
	// A proxy would dial in its place, past the guard.
	transport.Proxy = nil
	return &http.Client{Transport: observability.HTTPTransport(transport), Timeout: documentTimeout}
}

// client is a public client: one this server registered (RFC 7591), or one whose client_id is the URL of its
// metadata document (CIMD). Everything op reads of an OpenID Connect client is inert, since no id token is issued.
type client struct {
	id       string
	metadata oauthex.ClientRegistrationMetadata
}

// client resolves a client_id. A refusal is op's invalid_client; a fault of the database is an outage.
func (s *Server) client(ctx context.Context, id string) (*client, error) {
	if strings.HasPrefix(id, "https://") {
		return s.documentClient(ctx, id)
	}
	var document []byte
	err := s.db.QueryRowContext(ctx, `SELECT metadata FROM oauth_registered_client WHERE client_id = $1`, id).Scan(&document)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		return nil, oidc.ErrInvalidClient().WithDescription("unknown client_id")
	case err != nil:
		return nil, unavailable(err)
	}
	c := &client{id: id}
	if err := json.Unmarshal(document, &c.metadata); err != nil {
		return nil, fmt.Errorf("stored registration of client %q: %w", id, err)
	}
	return c, nil
}

// documentClient fetches and checks the client metadata document a client_id names, each time it is looked up.
func (s *Server) documentClient(ctx context.Context, id string) (*client, error) {
	refuse := func(description string, err error) (*client, error) {
		return nil, oidc.ErrInvalidClient().WithDescription("%s", description).WithParent(err)
	}
	if err := validateClientIDURL(id); err != nil {
		return refuse("client_id must be the https URL of a client metadata document", err)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, id, nil)
	if err != nil {
		return refuse("client_id must be the https URL of a client metadata document", err)
	}
	request.Header.Set("Accept", "application/json")
	response, err := s.documents.Do(request)
	if err != nil {
		return refuse("the client metadata document could not be fetched", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return refuse("the client metadata document could not be fetched", fmt.Errorf("HTTP %s", response.Status))
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, maxDocumentBytes+1))
	switch {
	case err != nil:
		return refuse("the client metadata document could not be fetched", err)
	case len(body) > maxDocumentBytes:
		return refuse(fmt.Sprintf("the client metadata document exceeds %d bytes", maxDocumentBytes), nil)
	}
	var document struct {
		ClientID string `json:"client_id"`
		oauthex.ClientRegistrationMetadata
	}
	if err := json.Unmarshal(body, &document); err != nil {
		return refuse("the client metadata document is not JSON client metadata", err)
	}
	if document.ClientID != id {
		return refuse("the client metadata document names another client_id", nil)
	}
	if method := document.TokenEndpointAuthMethod; method != "" && method != string(oidc.AuthMethodNone) {
		return refuse("only public clients, with token_endpoint_auth_method none, are supported", nil)
	}
	if err := normalize(&document.ClientRegistrationMetadata); err != nil {
		return refuse(err.Description, nil)
	}
	return &client{id: id, metadata: document.ClientRegistrationMetadata}, nil
}

// validateClientIDURL applies the CIMD draft's rules for an https client identifier URL.
func validateClientIDURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return err
	}
	switch {
	case u.User != nil, strings.Contains(raw, "#"):
		return fmt.Errorf("%q has credentials or a fragment", raw)
	case u.Path == "" || u.Path == "/":
		return fmt.Errorf("%q has no path", raw)
	case slices.ContainsFunc(strings.Split(u.Path, "/"), func(segment string) bool { return segment == "." || segment == ".." }):
		return fmt.Errorf("%q has a dot segment", raw)
	}
	return nil
}

// registrationError is a refused client registration (RFC 7591 section 3.2.2).
type registrationError struct {
	Code        string `json:"error"`
	Description string `json:"error_description"`
}

// normalize checks client metadata, registered or published, and fills in RFC 7591's defaults. A client must take the
// code flow, and only to redirect URIs a code can safely be sent to: https, http on a loopback host, or a private-use
// scheme of a native app. Of its grant types it keeps the code flow and refresh: Claude's metadata document also lists
// jwt-bearer, which this server does not serve, and a registration is answered with the grant types kept (RFC 7591
// section 3.2.1).
func normalize(metadata *oauthex.ClientRegistrationMetadata) *registrationError {
	if len(metadata.RedirectURIs) == 0 {
		return &registrationError{Code: "invalid_redirect_uri", Description: "redirect_uris is required"}
	}
	for _, uri := range metadata.RedirectURIs {
		if !redirectable(uri) {
			return &registrationError{Code: "invalid_redirect_uri", Description: fmt.Sprintf("redirect URI %q is not allowed", uri)}
		}
	}
	if len(metadata.GrantTypes) == 0 {
		metadata.GrantTypes = []string{string(oidc.GrantTypeCode)}
	}
	metadata.GrantTypes = slices.DeleteFunc(metadata.GrantTypes, func(grant string) bool {
		return grant != string(oidc.GrantTypeCode) && grant != string(oidc.GrantTypeRefreshToken)
	})
	if len(metadata.ResponseTypes) == 0 {
		metadata.ResponseTypes = []string{string(oidc.ResponseTypeCode)}
	}
	switch {
	case !slices.Contains(metadata.GrantTypes, string(oidc.GrantTypeCode)):
		return &registrationError{Code: "invalid_client_metadata", Description: "grant_types must include authorization_code"}
	case slices.ContainsFunc(metadata.ResponseTypes, func(response string) bool { return response != string(oidc.ResponseTypeCode) }):
		return &registrationError{Code: "invalid_client_metadata", Description: "response_types may be code only"}
	}
	return nil
}

// redirectable is whether a code may be sent to the redirect URI: an absolute URI with no fragment, https, http on a
// loopback host, or a scheme no browser runs as script or reads from disk.
func redirectable(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil || !u.IsAbs() || strings.Contains(raw, "#") {
		return false
	}
	switch strings.ToLower(u.Scheme) {
	case "https":
		return u.Host != ""
	case "http":
		_, loopback := op.HTTPLoopbackOrLocalhost(raw)
		return loopback
	case "javascript", "data", "vbscript", "file", "blob", "about":
		return false
	}
	return true
}

// register is Dynamic Client Registration (RFC 7591). Every client registers public: a requested
// token_endpoint_auth_method is replaced by none, which RFC 7591 section 3.2.1 lets a server do by answering the
// value it registered.
func (s *Server) register(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	answer := func(status int, body any) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(body)
	}
	var metadata oauthex.ClientRegistrationMetadata
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxDocumentBytes)).Decode(&metadata); err != nil {
		answer(http.StatusBadRequest, &registrationError{Code: "invalid_client_metadata", Description: "the body is not JSON client metadata"})
		return
	}
	metadata.TokenEndpointAuthMethod = string(oidc.AuthMethodNone)
	if refused := normalize(&metadata); refused != nil {
		answer(http.StatusBadRequest, refused)
		return
	}
	document, err := json.Marshal(metadata)
	if err != nil {
		op.WriteError(w, r, err, nil)
		return
	}
	id := rand.Text()
	if _, err := s.db.ExecContext(r.Context(),
		`INSERT INTO oauth_registered_client (client_id, metadata) VALUES ($1, $2)`, id, document); err != nil {
		op.WriteError(w, r, unavailable(err), nil)
		return
	}
	answer(http.StatusCreated, &oauthex.ClientRegistrationResponse{
		ClientRegistrationMetadata: metadata, ClientID: id, ClientIDIssuedAt: time.Now(),
	})
}

func (c *client) GetID() string                    { return c.id }
func (c *client) RedirectURIs() []string           { return c.metadata.RedirectURIs }
func (c *client) PostLogoutRedirectURIs() []string { return nil }
func (c *client) AuthMethod() oidc.AuthMethod      { return oidc.AuthMethodNone }

// LoginURL is unused: Authorize sends the browser to consent itself.
func (c *client) LoginURL(string) string               { return "" }
func (c *client) AccessTokenType() op.AccessTokenType  { return op.AccessTokenTypeBearer }
func (c *client) IDTokenLifetime() time.Duration       { return 0 }
func (c *client) DevMode() bool                        { return false }
func (c *client) IsScopeAllowed(scope string) bool     { return scope == scopeActions }
func (c *client) IDTokenUserinfoClaimsAssertion() bool { return false }
func (c *client) ClockSkew() time.Duration             { return 0 }

func (c *client) RestrictAdditionalIdTokenScopes() func([]string) []string {
	return func(scopes []string) []string { return scopes }
}

func (c *client) RestrictAdditionalAccessTokenScopes() func([]string) []string {
	return func(scopes []string) []string { return scopes }
}

// ApplicationType is native when a redirect URI is loopback or a private-use scheme, so op accepts a loopback
// redirect on any port (RFC 8252 section 7.3).
func (c *client) ApplicationType() op.ApplicationType {
	for _, uri := range c.metadata.RedirectURIs {
		if _, loopback := op.HTTPLoopbackOrLocalhost(uri); loopback || !strings.HasPrefix(uri, "https://") {
			return op.ApplicationTypeNative
		}
	}
	return op.ApplicationTypeWeb
}

func (c *client) ResponseTypes() []oidc.ResponseType {
	types := make([]oidc.ResponseType, len(c.metadata.ResponseTypes))
	for i, t := range c.metadata.ResponseTypes {
		types[i] = oidc.ResponseType(t)
	}
	return types
}

func (c *client) GrantTypes() []oidc.GrantType {
	types := make([]oidc.GrantType, len(c.metadata.GrantTypes))
	for i, t := range c.metadata.GrantTypes {
		types[i] = oidc.GrantType(t)
	}
	return types
}

// name is the name the client gives itself, if any.
func (c *client) name() *string {
	if c.metadata.ClientName == "" {
		return nil
	}
	return &c.metadata.ClientName
}
