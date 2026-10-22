package linkage_test

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/util/testing/nettest"
)

// answer is what a fake MCP server or authorization server answers at a path; every other path answers 404.
type answer struct {
	status int
	header http.Header
	body   any
}

func document(body any) answer { return answer{status: http.StatusOK, body: body} }

// The servers' answers are shaped after those of the MCP servers the deployments link.
func TestDiscoveryFindsTheEndpointsResourceAndScopesALinkRequests(t *testing.T) {
	const configuredAuthorization, configuredToken = "https://test-configured.example/authorize", "https://test-configured.example/token"
	for _, tc := range []struct {
		name string
		// path is the MCP server's path at the fake.
		path    string
		answers func(base string) map[string]answer
		// configureResource is the server's configured resource; empty for none.
		configureResource                                     string
		wantAuthorization, wantToken, wantResource, wantScope string
	}{{
		name: "protected resource metadata of a doubled content type, as GitHub's",
		path: "/mcp/",
		answers: func(base string) map[string]answer {
			return map[string]answer{
				"/mcp/": {status: http.StatusUnauthorized},
				"/.well-known/oauth-protected-resource/mcp/": {
					status: http.StatusOK,
					header: http.Header{"Content-Type": {"application/json,application/json"}},
					body: map[string]any{
						"resource": base + "/mcp/", "authorization_servers": []string{base + "/login/oauth"},
						"scopes_supported": []string{"test-repo", "test-gist"},
					},
				},
				"/.well-known/oauth-authorization-server/login/oauth": document(map[string]any{
					"issuer":                 base + "/login/oauth",
					"authorization_endpoint": base + "/login/oauth/authorize", "token_endpoint": base + "/login/oauth/access_token",
					"scopes_supported": []string{"test-unused"},
				}),
			}
		},
		wantAuthorization: "/login/oauth/authorize", wantToken: "/login/oauth/access_token", wantResource: "/mcp/",
		wantScope: "test-repo test-gist",
	}, {
		name: "metadata without a resource, and another issuer's at the server's origin, as the Kubernetes MCP server's",
		path: "/mcp",
		answers: func(base string) map[string]answer {
			return map[string]answer{
				"/mcp": {status: http.StatusUnauthorized, header: http.Header{
					"Www-Authenticate": {`Bearer realm="test-realm", audience="test-audience", error="missing_token"`},
				}},
				"/.well-known/oauth-protected-resource/mcp": {status: http.StatusServiceUnavailable},
				"/.well-known/oauth-protected-resource": document(map[string]any{
					"authorization_servers": []string{base},
					"scopes_supported":      []string{"test-unused"},
				}),
				"/.well-known/oauth-authorization-server": document(map[string]any{
					"issuer": "https://test-idp.example/application/o/test/", "authorization_endpoint": "https://test-idp.example/authorize/",
					"token_endpoint": "https://test-idp.example/token/", "scopes_supported": []string{"openid", "test-offline"},
				}),
			}
		},
		wantAuthorization: "https://test-idp.example/authorize/", wantToken: "https://test-idp.example/token/",
		wantResource: "/mcp", wantScope: "openid test-offline",
	}, {
		name: "the metadata the server names, OpenID Connect's issuer metadata, and the scope the server asks for",
		path: "/mcp",
		answers: func(base string) map[string]answer {
			return map[string]answer{
				"/mcp": {status: http.StatusUnauthorized, header: http.Header{
					"Www-Authenticate": {`Bearer resource_metadata="` + base + `/test-metadata", scope="test-challenged"`},
				}},
				"/test-metadata": document(map[string]any{
					"resource": base, "authorization_servers": []string{base + "/test-issuer"},
					"scopes_supported": []string{"test-unused"},
				}),
				"/.well-known/openid-configuration/test-issuer": document(map[string]any{
					"issuer":                 base + "/test-issuer",
					"authorization_endpoint": base + "/test-issuer/authorize", "token_endpoint": base + "/test-issuer/token",
				}),
			}
		},
		wantAuthorization: "/test-issuer/authorize", wantToken: "/test-issuer/token", wantResource: "", wantScope: "test-challenged",
	}, {
		name: "a resource that does not cover the server, and no issuer metadata",
		path: "/mcp",
		answers: func(base string) map[string]answer {
			return map[string]answer{
				"/.well-known/oauth-protected-resource/mcp": document(map[string]any{
					"resource":              base + "/test-other",
					"authorization_servers": []string{base},
				}),
			}
		},
		configureResource: "https://test-resource.example/mcp",
		wantAuthorization: configuredAuthorization, wantToken: configuredToken, wantResource: "https://test-resource.example/mcp",
	}, {
		name: "an issuer metadata URL that fails other than 4xx ends the search",
		path: "/mcp",
		answers: func(base string) map[string]answer {
			return map[string]answer{
				"/.well-known/oauth-protected-resource": document(map[string]any{
					"resource":              base,
					"authorization_servers": []string{base},
				}),
				"/.well-known/oauth-authorization-server": {status: http.StatusInternalServerError},
				"/.well-known/openid-configuration": document(map[string]any{
					"issuer":                 base,
					"authorization_endpoint": base + "/unused", "token_endpoint": base + "/unused",
				}),
			}
		},
		wantAuthorization: configuredAuthorization, wantToken: configuredToken, wantResource: "",
	}} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture(t)
			var answers map[string]answer
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				found, ok := answers[r.URL.Path]
				if !ok {
					http.NotFound(w, r)
					return
				}
				for key, values := range found.header {
					w.Header()[key] = values
				}
				if w.Header().Get("Content-Type") == "" {
					w.Header().Set("Content-Type", "application/json")
				}
				w.WriteHeader(found.status)
				if found.body != nil {
					_ = json.NewEncoder(w).Encode(found.body)
				}
			}))
			t.Cleanup(upstream.Close)
			answers = tc.answers(upstream.URL)
			server := linkage.Server{
				ServerID: testServer, ServerURL: upstream.URL + tc.path, AuthorizationEndpoint: new(configuredAuthorization),
				TokenEndpoint: new(configuredToken), ClientID: new("test-client"), RedirectURI: "https://test-actions.example/callback",
			}
			if tc.configureResource != "" {
				server.Resource = &tc.configureResource
			}
			started, err := f.authority(t, server).Start(t.Context(), testServer, nil, operator)
			if err != nil {
				t.Fatal(err)
			}
			authorization := must(url.Parse(started.GetAuthorizationUrl()))
			query := authorization.Query()
			authorization.RawQuery = ""
			var token string
			if err := f.db.QueryRowContext(t.Context(), "SELECT token_endpoint FROM mcp_linkage_flow").Scan(&token); err != nil {
				t.Fatal(err)
			}
			for _, check := range []struct{ what, got, want string }{
				{"authorization endpoint", authorization.String(), resolve(upstream.URL, tc.wantAuthorization)},
				{"token endpoint", token, resolve(upstream.URL, tc.wantToken)},
				{"resource", query.Get("resource"), resolve(upstream.URL, tc.wantResource)},
				{"scope", query.Get("scope"), tc.wantScope},
			} {
				if check.got != check.want {
					t.Errorf("the %s is %q, want %q", check.what, check.got, check.want)
				}
			}
		})
	}
}

// resolve is want, a path at the fake when it is not a URL.
func resolve(base, want string) string {
	if parsed := must(url.Parse(want)); parsed.IsAbs() {
		return want
	}
	return base + want
}

func TestAServerWhoseMetadataCannotBeUsedCannotBeLinked(t *testing.T) {
	f := newFixture(t)
	silent := httptest.NewServer(http.NotFoundHandler())
	t.Cleanup(silent.Close)
	unreachable := "http://" + nettest.RefusingAddress(t)
	for name, server := range map[string]linkage.Server{
		"no metadata and no configured endpoints": {
			ServerID: testServer, ServerURL: silent.URL + "/mcp",
			ClientID: new("test-client"), RedirectURI: "https://test-actions.example/callback",
		},
		"unreachable": {
			ServerID: testServer, ServerURL: unreachable + "/mcp", ClientID: new("test-client"),
			RedirectURI: "https://test-actions.example/callback", AuthorizationEndpoint: new("https://test-configured.example/authorize"),
			TokenEndpoint: new("https://test-configured.example/token"),
		},
	} {
		var unusable *linkage.UnusableError
		if _, err := f.authority(t, server).Start(t.Context(), testServer, nil, operator); !errors.As(err, &unusable) {
			t.Errorf("%s: %v", name, err)
		}
	}
	var flows int
	if err := f.db.QueryRowContext(t.Context(), "SELECT count(*) FROM mcp_linkage_flow").Scan(&flows); err != nil || flows != 0 {
		t.Errorf("%d flows started, %v", flows, err)
	}
}
