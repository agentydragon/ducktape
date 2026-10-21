package oauthspike

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"time"

	"github.com/zitadel/oidc/v3/pkg/oidc"
	"github.com/zitadel/oidc/v3/pkg/op"
)

const (
	// maxDocumentBytes bounds a fetched client metadata document; the CIMD draft suggests 5 KB.
	maxDocumentBytes = 5 << 10
	// documentTTL is how long a fetched document answers op's client lookups before it is fetched again.
	documentTTL = 5 * time.Minute
	// scopeActions is the one scope this server grants beyond the OIDC ones op always keeps.
	scopeActions = "actions"
)

// clientDocument is the part of a Client ID Metadata Document (draft-ietf-oauth-client-id-metadata-document) this
// server reads.
type clientDocument struct {
	ClientID                string   `json:"client_id"`
	RedirectURIs            []string `json:"redirect_uris"`
	GrantTypes              []string `json:"grant_types,omitempty"`
	ResponseTypes           []string `json:"response_types,omitempty"`
	TokenEndpointAuthMethod string   `json:"token_endpoint_auth_method,omitempty"`
}

// clientRegistry resolves op's client lookups: a client_id is the https URL of the client's metadata document,
// fetched and validated here and kept in oauth_client for documentTTL.
type clientRegistry struct {
	db *sql.DB
	// fetch follows no redirects. Production passes one whose dialer refuses private addresses (code.dny.dev/ssrf, as
	// the egress proxy does); the tests pass the TLS test server's client.
	fetch *http.Client
}

func (r *clientRegistry) client(ctx context.Context, clientID string) (op.Client, error) {
	if err := validateClientIDURL(clientID); err != nil {
		return nil, oidc.ErrInvalidClient().WithDescription("client_id must be the https URL of a client metadata document").WithParent(err)
	}
	var raw []byte
	err := r.db.QueryRowContext(
		ctx,
		`SELECT document FROM oauth_client WHERE client_id = $1 AND fetched_at > now() - make_interval(secs => $2)`,
		clientID, documentTTL.Seconds(),
	).Scan(&raw)
	if err == nil {
		return parseClientDocument(clientID, raw)
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	raw, err = r.fetchDocument(ctx, clientID)
	if err != nil {
		return nil, oidc.ErrInvalidClient().WithDescription("the client metadata document could not be fetched").WithParent(err)
	}
	client, err := parseClientDocument(clientID, raw)
	if err != nil {
		return nil, err
	}
	if _, err := r.db.ExecContext(
		ctx, `
		INSERT INTO oauth_client (client_id, document, fetched_at) VALUES ($1, $2, now())
		ON CONFLICT (client_id) DO UPDATE SET document = EXCLUDED.document, fetched_at = EXCLUDED.fetched_at`,
		clientID, raw,
	); err != nil {
		return nil, err
	}
	return client, nil
}

func (r *clientRegistry) fetchDocument(ctx context.Context, clientID string) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, clientID, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", "application/json")
	response, err := r.fetch.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("fetch %s: HTTP %d", clientID, response.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, maxDocumentBytes+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxDocumentBytes {
		return nil, fmt.Errorf("fetch %s: document exceeds %d bytes", clientID, maxDocumentBytes)
	}
	return body, nil
}

// validateClientIDURL applies the CIMD draft's rules for a client identifier URL.
func validateClientIDURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return err
	}
	switch {
	case u.Scheme != "https":
		return fmt.Errorf("%q is not https", raw)
	case u.User != nil, strings.Contains(raw, "#"):
		return fmt.Errorf("%q has credentials or a fragment", raw)
	case u.Path == "" || u.Path == "/":
		return fmt.Errorf("%q has no path", raw)
	}
	if slices.ContainsFunc(strings.Split(u.Path, "/"), func(segment string) bool { return segment == "." || segment == ".." }) {
		return fmt.Errorf("%q has a dot segment", raw)
	}
	return nil
}

func parseClientDocument(clientID string, raw []byte) (*cimdClient, error) {
	var document clientDocument
	if err := json.Unmarshal(raw, &document); err != nil {
		return nil, oidc.ErrInvalidClient().WithDescription("the client metadata document is not JSON").WithParent(err)
	}
	switch {
	case document.ClientID != clientID:
		return nil, oidc.ErrInvalidClient().WithDescription("the client metadata document names another client_id")
	case len(document.RedirectURIs) == 0:
		return nil, oidc.ErrInvalidClient().WithDescription("the client metadata document has no redirect_uris")
	case document.TokenEndpointAuthMethod != "" && document.TokenEndpointAuthMethod != string(oidc.AuthMethodNone):
		return nil, oidc.ErrInvalidClient().WithDescription("only public clients (token_endpoint_auth_method none) are supported")
	}
	return &cimdClient{document: document}, nil
}

// cimdClient presents a metadata document as op's Client. op validates redirect_uri, response_type and grant_type
// against it; everything id-token shaped is inert because Server never issues one.
type cimdClient struct {
	document clientDocument
}

func (c *cimdClient) GetID() string                        { return c.document.ClientID }
func (c *cimdClient) RedirectURIs() []string               { return c.document.RedirectURIs }
func (c *cimdClient) PostLogoutRedirectURIs() []string     { return nil }
func (c *cimdClient) AuthMethod() oidc.AuthMethod          { return oidc.AuthMethodNone }
func (c *cimdClient) LoginURL(string) string               { return "" } // Server.Authorize sends the browser to consent itself.
func (c *cimdClient) AccessTokenType() op.AccessTokenType  { return op.AccessTokenTypeBearer }
func (c *cimdClient) IDTokenLifetime() time.Duration       { return 0 }
func (c *cimdClient) DevMode() bool                        { return false }
func (c *cimdClient) IsScopeAllowed(scope string) bool     { return scope == scopeActions }
func (c *cimdClient) IDTokenUserinfoClaimsAssertion() bool { return false }
func (c *cimdClient) ClockSkew() time.Duration             { return 0 }

func (c *cimdClient) RestrictAdditionalIdTokenScopes() func([]string) []string {
	return func(scopes []string) []string { return scopes }
}

func (c *cimdClient) RestrictAdditionalAccessTokenScopes() func([]string) []string {
	return func(scopes []string) []string { return scopes }
}

// ApplicationType is native when a redirect URI is loopback or a custom scheme, so op accepts a loopback redirect
// on any port (RFC 8252); a CIMD document carries no application_type.
func (c *cimdClient) ApplicationType() op.ApplicationType {
	for _, uri := range c.document.RedirectURIs {
		if _, loopback := op.HTTPLoopbackOrLocalhost(uri); loopback || !strings.HasPrefix(uri, "https://") {
			return op.ApplicationTypeNative
		}
	}
	return op.ApplicationTypeWeb
}

func (c *cimdClient) ResponseTypes() []oidc.ResponseType {
	if len(c.document.ResponseTypes) == 0 {
		return []oidc.ResponseType{oidc.ResponseTypeCode}
	}
	types := make([]oidc.ResponseType, len(c.document.ResponseTypes))
	for i, t := range c.document.ResponseTypes {
		types[i] = oidc.ResponseType(t)
	}
	return types
}

func (c *cimdClient) GrantTypes() []oidc.GrantType {
	if len(c.document.GrantTypes) == 0 {
		return []oidc.GrantType{oidc.GrantTypeCode}
	}
	types := make([]oidc.GrantType, len(c.document.GrantTypes))
	for i, t := range c.document.GrantTypes {
		types[i] = oidc.GrantType(t)
	}
	return types
}
