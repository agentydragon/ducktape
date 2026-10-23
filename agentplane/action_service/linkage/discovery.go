package linkage

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/oauthex"
)

// metadataLimit bounds a metadata document.
const metadataLimit = 1 << 20

// discovered is what a server's metadata says a link to it needs.
type discovered struct {
	authorizationEndpoint, tokenEndpoint string
	// resource is the RFC 8707 resource a token is requested for.
	resource string
	// scopes are those a link may request.
	scopes []string
	// cimd is whether the authorization server takes a Client ID Metadata Document's URL as a client_id.
	cimd bool
}

// discover reads RFC 9728 protected resource metadata, then RFC 8414 authorization server metadata, falling back to
// the endpoints the settings configure.
//
// It fetches the documents itself and decodes them into oauthex's types. The SDK's own fetches refuse servers this
// service links: GitHub's MCP server answers its protected resource metadata as `application/json,application/json`,
// the Kubernetes MCP server (containers/kubernetes-mcp-server v0.0.66) serves Authentik's metadata, whose issuer is
// Authentik's, at its own origin, and the testing deployment's MCP server is plain HTTP inside the cluster.
func (a *Authority) discover(ctx context.Context, server Server) (discovered, error) {
	started := time.Now()
	found, outcome, err := a.discoverMetadata(ctx, server)
	observe("discovery", outcome, started)
	return found, err
}

func (a *Authority) discoverMetadata(ctx context.Context, server Server) (discovered, string, error) {
	failed := func(err error) (discovered, string, error) {
		slog.WarnContext(ctx, "MCP OAuth metadata discovery failed", "server", server.ServerID, "error", err)
		return discovered{}, "transport", &UnusableError{Reason: "MCP OAuth metadata discovery failed"}
	}
	serverURL, err := url.Parse(server.ServerURL)
	if err != nil {
		return failed(err)
	}
	probe, err := a.get(ctx, server.ServerURL)
	if err != nil {
		return failed(err)
	}
	probe.Body.Close()
	challenge := challengeOf(ctx, server, probe)

	var resource *oauthex.ProtectedResourceMetadata
	for _, candidate := range resourceMetadataURLs(challenge.resourceMetadata, serverURL) {
		var document oauthex.ProtectedResourceMetadata
		status, decoded, err := a.fetch(ctx, candidate, &document)
		if err != nil {
			return failed(err)
		}
		// RFC 9728 requires both fields; a document without them is skipped, as the Python SDK's model refuses it.
		if status == http.StatusOK && decoded && document.Resource != "" && len(document.AuthorizationServers) > 0 {
			resource = &document
			break
		}
	}

	candidates := []string{origin(serverURL) + "/.well-known/oauth-authorization-server"}
	if resource != nil {
		issuer, err := url.Parse(resource.AuthorizationServers[0])
		if err != nil {
			return failed(err)
		}
		candidates = authServerMetadataURLs(issuer)
	}
	var metadata *oauthex.AuthServerMeta
	for _, candidate := range candidates {
		var document oauthex.AuthServerMeta
		status, decoded, err := a.fetch(ctx, candidate, &document)
		if err != nil {
			return failed(err)
		}
		if status == http.StatusOK && decoded && httpURL(document.Issuer) &&
			httpURL(document.AuthorizationEndpoint) && httpURL(document.TokenEndpoint) {
			metadata = &document
			break
		}
		// A failure other than 4xx ends the search, as the Python SDK's does.
		if status != http.StatusOK && (status < 400 || status >= 500) {
			break
		}
	}

	var found discovered
	if server.AuthorizationEndpoint != nil && server.TokenEndpoint != nil {
		found.authorizationEndpoint, found.tokenEndpoint = *server.AuthorizationEndpoint, *server.TokenEndpoint
	}
	if metadata != nil {
		found.authorizationEndpoint, found.tokenEndpoint = metadata.AuthorizationEndpoint, metadata.TokenEndpoint
		found.cimd = metadata.ClientIDMetadataDocumentSupported
	}
	if found.authorizationEndpoint == "" || found.tokenEndpoint == "" {
		return discovered{}, "rejected", &UnusableError{Reason: "MCP OAuth metadata did not provide authorization and token endpoints"}
	}

	requested := canonicalResource(*serverURL)
	found.resource = requested
	if server.Resource != nil && *server.Resource != "" {
		found.resource = *server.Resource
	}
	if resource != nil && resourceAllowed(requested, resource.Resource) {
		found.resource = resource.Resource
	}

	switch {
	case len(server.Scopes) > 0:
		found.scopes = server.Scopes
	case challenge.scope != "":
		found.scopes = strings.Fields(challenge.scope)
	case resource != nil && resource.ScopesSupported != nil:
		found.scopes = resource.ScopesSupported
	case metadata != nil && metadata.ScopesSupported != nil:
		found.scopes = metadata.ScopesSupported
	}
	return found, "success", nil
}

func (a *Authority) get(ctx context.Context, target string) (*http.Response, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	return a.discovery.Do(request)
}

// fetch GETs target and, when it answers 200, decodes its JSON body into into. It fails only when target could not
// be asked; a body that does not decode is reported as such, whatever its Content-Type says.
func (a *Authority) fetch(ctx context.Context, target string, into any) (status int, decoded bool, err error) {
	response, err := a.get(ctx, target)
	if err != nil {
		return 0, false, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return response.StatusCode, false, nil
	}
	return response.StatusCode, json.NewDecoder(io.LimitReader(response.Body, metadataLimit)).Decode(into) == nil, nil
}

// challenge is what the server's answer to an unauthenticated request says about its authorization.
type challenge struct {
	// resourceMetadata is the URL of its protected resource metadata, which only a 401 names; empty when none.
	resourceMetadata string
	// scope is the space-separated scopes it asks for; empty when none.
	scope string
}

func challengeOf(ctx context.Context, server Server, probe *http.Response) challenge {
	challenges, err := oauthex.ParseWWWAuthenticate(probe.Header.Values("WWW-Authenticate"))
	if err != nil {
		// The Python SDK finds parameters in a malformed header by pattern; none is taken from one here.
		slog.WarnContext(ctx, "MCP server sent a malformed WWW-Authenticate header", "server", server.ServerID, "error", err)
		return challenge{}
	}
	var found challenge
	for _, c := range challenges {
		if found.resourceMetadata == "" && probe.StatusCode == http.StatusUnauthorized {
			found.resourceMetadata = c.Params["resource_metadata"]
		}
		if found.scope == "" {
			found.scope = c.Params["scope"]
		}
	}
	return found
}

func origin(u *url.URL) string { return u.Scheme + "://" + u.Host }

// resourceMetadataURLs are where the server's protected resource metadata may be, in the order RFC 9728 and the MCP
// specification try them: the URL the server named, then the well-known URL with the server's path, then without.
func resourceMetadataURLs(named string, server *url.URL) []string {
	var urls []string
	if named != "" {
		urls = append(urls, named)
	}
	if path := server.EscapedPath(); path != "" && path != "/" {
		urls = append(urls, origin(server)+"/.well-known/oauth-protected-resource"+path)
	}
	return append(urls, origin(server)+"/.well-known/oauth-protected-resource")
}

// authServerMetadataURLs are where the metadata of the authorization server issuer may be: RFC 8414's well-known URL,
// then OpenID Connect discovery's, each with the issuer's path inserted, and OpenID Connect's appended to the path.
func authServerMetadataURLs(issuer *url.URL) []string {
	path := strings.TrimRight(issuer.EscapedPath(), "/")
	if path == "" {
		return []string{
			origin(issuer) + "/.well-known/oauth-authorization-server", origin(issuer) + "/.well-known/openid-configuration",
		}
	}
	return []string{
		origin(issuer) + "/.well-known/oauth-authorization-server" + path,
		origin(issuer) + "/.well-known/openid-configuration" + path,
		origin(issuer) + path + "/.well-known/openid-configuration",
	}
}

// canonicalResource is the server's URL as an RFC 8707 resource: its scheme and host lower case, without a fragment.
func canonicalResource(server url.URL) string {
	server.Host = strings.ToLower(server.Host)
	server.Fragment, server.RawFragment = "", ""
	return server.String()
}

// resourceAllowed is whether a token for the resource named covers the requested one: the same origin, and a path at
// or under the named path.
func resourceAllowed(requested, named string) bool {
	r, err := url.Parse(requested)
	if err != nil {
		return false
	}
	n, err := url.Parse(named)
	if err != nil || !strings.EqualFold(r.Scheme, n.Scheme) || !strings.EqualFold(r.Host, n.Host) {
		return false
	}
	requestedPath, namedPath := r.EscapedPath(), n.EscapedPath()
	if len(requestedPath) < len(namedPath) {
		return false
	}
	return strings.HasPrefix(strings.TrimSuffix(requestedPath, "/")+"/", strings.TrimSuffix(namedPath, "/")+"/")
}
