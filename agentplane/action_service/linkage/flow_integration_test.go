package linkage_test

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/testing/oauthprovider"
	"google.golang.org/protobuf/encoding/protojson"
)

const testServer = "test-server"

// link starts a flow of the server and finishes it with a code the provider grants.
func link(t *testing.T, authority *linkage.Authority, serverID string) *operator_action_servicepb.McpServerLinkage {
	t.Helper()
	started, err := authority.Start(t.Context(), serverID, nil, operator)
	if err != nil {
		t.Fatal(err)
	}
	linked, err := authority.Callback(t.Context(), oauthprovider.State(t, started.GetAuthorizationUrl()), "test-code")
	if err != nil {
		t.Fatal(err)
	}
	return linked
}

// routes serves the authority's public routes.
func routes(t *testing.T, authority *linkage.Authority) string {
	t.Helper()
	mux := http.NewServeMux()
	authority.Mount(mux)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server.URL
}

func get(t *testing.T, target string) (*http.Response, []byte) {
	t.Helper()
	response, err := http.Get(target)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response, body
}

func callback(t *testing.T, base, state, code string) (int, []byte) {
	t.Helper()
	response, body := get(t, base+linkage.CallbackPath+"?"+url.Values{"state": {state}, "code": {code}}.Encode())
	return response.StatusCode, body
}

func TestTheAuthorizationURLKeepsTheEndpointsQueryButTheFlowsParameters(t *testing.T) {
	for name, existing := range map[string]string{
		"no query":   "",
		"with query": "?tenant=a%2Bb%26c&tag=one&tag=two&empty=&state=stale&client_id=stale",
	} {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			provider := oauthprovider.New(t, oauthprovider.Options{})
			server := provider.Server(testServer)
			server.AuthorizationEndpoint = new(provider.URL + "/authorize" + existing)
			server.Scopes = []string{"test-read", "test-write"}
			authority := f.authority(t, server)
			started, err := authority.Start(t.Context(), testServer, nil, operator)
			if err != nil {
				t.Fatal(err)
			}
			authorization, err := url.Parse(started.GetAuthorizationUrl())
			if err != nil {
				t.Fatal(err)
			}
			if got := authorization.Scheme + "://" + authorization.Host + authorization.Path; got != provider.URL+"/authorize" {
				t.Errorf("the authorization endpoint is %s", got)
			}
			query := authorization.Query()
			want := url.Values{
				"response_type": {"code"}, "client_id": {oauthprovider.ClientID}, "redirect_uri": {oauthprovider.RedirectURI},
				"scope": {"test-read test-write"}, "code_challenge_method": {"S256"}, "resource": {server.ServerURL},
			}
			if existing != "" {
				want["tenant"], want["tag"], want["empty"] = []string{"a+b&c"}, []string{"one", "two"}, []string{""}
			}
			for key, values := range want {
				if !slices.Equal(query[key], values) {
					t.Errorf("%s is %q, want %q", key, query[key], values)
				}
			}
			if state := query["state"]; len(state) != 1 || state[0] == "stale" {
				t.Errorf("state is %q", state)
			}
			if challenge := query["code_challenge"]; len(challenge) != 1 || len(challenge[0]) != 43 {
				t.Errorf("code_challenge is %q", challenge)
			}
			linked, err := authority.Callback(t.Context(), query.Get("state"), "test-code")
			if err != nil {
				t.Fatal(err)
			}
			if linked.GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED {
				t.Errorf("the linkage is %v", linked)
			}
		})
	}
}

func TestALinkReplacesTheTokenAndADisconnectDiscardsIt(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, oauthprovider.Options{ExpiresIn: 3600})
	authority := f.authority(t, provider.Server(testServer))
	base := routes(t, authority)
	for i, want := range []struct {
		tokenRevision int
		token         string
	}{{1, "test-access-token-1"}, {2, "test-access-token-2"}, {1, "test-access-token-3"}} {
		if i == 2 {
			disconnected, err := authority.Disconnect(t.Context(), testServer)
			if err != nil {
				t.Fatal(err)
			}
			f.changed(t, "a disconnection")
			if disconnected.GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_UNLINKED {
				t.Errorf("the disconnected linkage is %v", disconnected)
			}
			if token, err := authority.Token(t.Context(), testServer); err != nil || token != nil {
				t.Errorf("a disconnected server's token is %+v, %v", token, err)
			}
		}
		started, err := authority.Start(t.Context(), testServer, nil, operator)
		if err != nil {
			t.Fatal(err)
		}
		state := oauthprovider.State(t, started.GetAuthorizationUrl())
		status, body := callback(t, base, state, "test-code")
		if status != http.StatusOK {
			t.Fatalf("callback %d: %s", status, body)
		}
		f.changed(t, "a link")
		if strings.Contains(string(body), want.token) {
			t.Error("the callback's answer carries the access token")
		}
		var linked operator_action_servicepb.McpServerLinkage
		if err := protojson.Unmarshal(body, &linked); err != nil {
			t.Fatal(err)
		}
		if linked.GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED {
			t.Errorf("link %d: the linkage is %v", i, &linked)
		}
		if revision := f.tokenRevision(t, testServer); revision != want.tokenRevision {
			t.Errorf("link %d: the token's revision is %d, want %d", i, revision, want.tokenRevision)
		}
		if token, err := authority.Token(t.Context(), testServer); err != nil || token.AccessToken != want.token {
			t.Errorf("link %d: the token is %+v, %v", i, token, err)
		}
		if status, _ := callback(t, base, state, "test-code"); status != http.StatusConflict {
			t.Errorf("link %d: a replayed callback answered %d", i, status)
		}
	}
}

func TestARefusedCodeIsReportedAndLeavesTheServerUnlinked(t *testing.T) {
	f := newFixture(t)
	authority := f.authority(t, oauthprovider.New(t, oauthprovider.Options{}).Server(testServer))
	started, err := authority.Start(t.Context(), testServer, nil, operator)
	if err != nil {
		t.Fatal(err)
	}
	status, body := callback(t, routes(t, authority), oauthprovider.State(t, started.GetAuthorizationUrl()), oauthprovider.RefusedCode)
	if status != http.StatusBadGateway || !strings.Contains(string(body), oauthprovider.RefusedError) {
		t.Errorf("the callback answered %d: %s", status, body)
	}
	if linked := linkageOf(t, authority, testServer); linked.GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_UNLINKED {
		t.Errorf("the linkage is %v", linked)
	}
}

func TestACallbackItCannotFinishIsRefused(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, oauthprovider.Options{})
	authority := f.authority(t, provider.Server(testServer))
	base := routes(t, authority)
	started, err := authority.Start(t.Context(), testServer, nil, operator)
	if err != nil {
		t.Fatal(err)
	}
	state := oauthprovider.State(t, started.GetAuthorizationUrl())
	if response, _ := get(t, base+linkage.CallbackPath+"?state="+state); response.StatusCode != http.StatusBadRequest {
		t.Errorf("a callback without a code answered %d", response.StatusCode)
	}
	if status, _ := callback(t, base, "test-unknown-state", "test-code"); status != http.StatusConflict {
		t.Errorf("a callback of an unknown flow answered %d", status)
	}
	if _, err := f.db.ExecContext(t.Context(), "UPDATE mcp_linkage_flow SET expires_at = now() - interval '1 second'"); err != nil {
		t.Fatal(err)
	}
	if status, _ := callback(t, base, state, "test-code"); status != http.StatusConflict {
		t.Errorf("a callback of an expired flow answered %d", status)
	}
	// A flow started before its server was removed from the settings.
	started, err = authority.Start(t.Context(), testServer, nil, operator)
	if err != nil {
		t.Fatal(err)
	}
	other := provider.Server("test-other-server")
	status, _ := callback(t, routes(t, f.authority(t, other)), oauthprovider.State(t, started.GetAuthorizationUrl()), "test-code")
	if status != http.StatusNotFound {
		t.Errorf("a callback of a removed server's flow answered %d", status)
	}
	if len(provider.Requests("authorization_code")) != 0 {
		t.Error("a refused callback exchanged its code")
	}
}

const testDocumentURL = "https://test-actions.example/oauth/client-metadata.json"

func cimdServer(provider *oauthprovider.Provider, serverID, redirectURI string) linkage.Server {
	server := provider.Server(serverID)
	server.ClientID, server.UseSharedCIMD, server.RedirectURI = nil, true, redirectURI
	return server
}

func TestTheSharedClientIDMetadataDocumentNamesEveryRedirectOfItsServers(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, oauthprovider.Options{})
	authority := f.authorityWith(t, linkage.ClientMetadata{URL: testDocumentURL, ClientName: "Test application"},
		cimdServer(provider, "test-first", "https://test-actions.example/other-callback"),
		cimdServer(provider, "test-second", oauthprovider.RedirectURI),
		cimdServer(provider, "test-third", oauthprovider.RedirectURI),
		provider.Server("test-registered"))
	response, body := get(t, routes(t, authority)+linkage.ClientMetadataPath)
	if response.StatusCode != http.StatusOK || response.Header.Get("Cache-Control") != "public, max-age=3600" {
		t.Errorf("the document answered %d with Cache-Control %q", response.StatusCode, response.Header.Get("Cache-Control"))
	}
	var document map[string]any
	if err := json.Unmarshal(body, &document); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{
		"client_id": testDocumentURL, "client_name": "Test application",
		// Sorted, each once.
		"redirect_uris":              []any{oauthprovider.RedirectURI, "https://test-actions.example/other-callback"},
		"token_endpoint_auth_method": "none", "grant_types": []any{"authorization_code", "refresh_token"},
		"response_types": []any{"code"},
	}
	if !reflect.DeepEqual(document, want) {
		t.Errorf("the document is %s", body)
	}

	none := f.authority(t, provider.Server(testServer))
	if response, _ := get(t, routes(t, none)+linkage.ClientMetadataPath); response.StatusCode != http.StatusNotFound {
		t.Errorf("without a shared client the document answered %d", response.StatusCode)
	}
}

func must[T any](value T, err error) T {
	if err != nil {
		panic(err)
	}
	return value
}

func TestASharedCIMDServerLinksAsTheDocumentsPublicClient(t *testing.T) {
	f := newFixture(t)
	metadata := linkage.ClientMetadata{URL: testDocumentURL, ClientName: "Test application"}
	provider := oauthprovider.New(t, oauthprovider.Options{Metadata: true, CIMD: true})
	authority := f.authorityWith(t, metadata, cimdServer(provider, testServer, oauthprovider.RedirectURI))
	started, err := authority.Start(t.Context(), testServer, nil, operator)
	if err != nil {
		t.Fatal(err)
	}
	if query := must(url.Parse(started.GetAuthorizationUrl())).Query(); query.Get("client_id") != testDocumentURL {
		t.Errorf("the authorization request's client_id is %q", query.Get("client_id"))
	}
	if _, err := authority.Callback(t.Context(), oauthprovider.State(t, started.GetAuthorizationUrl()), "test-code"); err != nil {
		t.Fatal(err)
	}
	exchange := provider.Requests("authorization_code")[0]
	if exchange.Get("client_id") != testDocumentURL || exchange.Has("client_secret") {
		t.Errorf("the exchange sent %v", exchange)
	}

	unsupported := oauthprovider.New(t, oauthprovider.Options{Metadata: true})
	authority = f.authorityWith(t, metadata, cimdServer(unsupported, testServer, oauthprovider.RedirectURI))
	var unusable *linkage.UnusableError
	if _, err := authority.Start(t.Context(), testServer, nil, operator); !errors.As(err, &unusable) {
		t.Errorf("a link through an authorization server without CIMD support: %v", err)
	}
}

func TestARegisteredClientSendsItsSecret(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, oauthprovider.Options{})
	server := provider.Server(testServer)
	secret := filepath.Join(t.TempDir(), "client-secret")
	if err := os.WriteFile(secret, []byte("test-client-secret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	server.ClientSecretFile = &secret
	link(t, f.authority(t, server), testServer)
	exchange := provider.Requests("authorization_code")[0]
	if exchange.Get("client_id") != oauthprovider.ClientID || exchange.Get("client_secret") != "test-client-secret" ||
		exchange.Get("code_verifier") == "" || exchange.Get("redirect_uri") != oauthprovider.RedirectURI {
		t.Errorf("the exchange sent %v", exchange)
	}
}

func TestALinkRequestsTheScopesAskedForAmongThoseAllowed(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, oauthprovider.Options{Metadata: true})
	server := provider.Server(testServer)
	server.AuthorizationEndpoint, server.TokenEndpoint = nil, nil
	authority := f.authority(t, server)
	for _, tc := range []struct {
		requested []string
		want      string
	}{
		// The metadata's, when the settings configure none.
		{nil, strings.Join(oauthprovider.Scopes, " ")},
		{[]string{"test-write", "test-write"}, "test-write"},
	} {
		started, err := authority.Start(t.Context(), testServer, tc.requested, operator)
		if err != nil {
			t.Fatal(err)
		}
		if scope := must(url.Parse(started.GetAuthorizationUrl())).Query().Get("scope"); scope != tc.want {
			t.Errorf("requesting %q requested %q", tc.requested, scope)
		}
	}
	if _, err := authority.Start(t.Context(), testServer, []string{"test-read", "test-admin"}, operator); !errors.Is(err, linkage.ErrUnconfiguredScopes) {
		t.Errorf("requesting a scope not allowed: %v", err)
	}
}

func TestAnUnconfiguredServerIsUnknown(t *testing.T) {
	f := newFixture(t)
	authority := f.authority(t, oauthprovider.New(t, oauthprovider.Options{}).Server(testServer))
	for name, call := range map[string]func() error{
		"Start":      func() error { _, err := authority.Start(t.Context(), "test-unknown", nil, operator); return err },
		"Disconnect": func() error { _, err := authority.Disconnect(t.Context(), "test-unknown"); return err },
		"Token":      func() error { _, err := authority.Token(t.Context(), "test-unknown"); return err },
	} {
		if err := call(); !errors.Is(err, linkage.ErrUnknownServer) {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestTheLinkagesAreTheConfiguredServersByID(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, oauthprovider.Options{ExpiresIn: 3600})
	second := provider.Server("test-second")
	second.Scopes = []string{"test-read"}
	authority := f.authority(t, second, provider.Server("test-first"))
	linkedAt := time.Now()
	link(t, authority, "test-first")
	linkages, lapse, err := authority.Linkages(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(linkages) != 2 || linkages[0].GetServerId() != "test-first" ||
		linkages[0].GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED || linkages[1].GetServerId() != "test-second" ||
		linkages[1].GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_UNLINKED ||
		!slices.Equal(linkages[1].GetScopes(), second.Scopes) || linkages[1].GetServerUrl() != second.ServerURL {
		t.Errorf("the linkages are %v", linkages)
	}
	// The linked token's expiry is when a view next changes with no commit.
	if lapse == nil || lapse.Sub(linkedAt) < time.Hour-time.Minute || lapse.Sub(linkedAt) > time.Hour+time.Minute {
		t.Errorf("the linkages next lapse at %v, an hour after %v", lapse, linkedAt)
	}
}

func TestRemovingAServerFromTheSettingsDiscardsItsLinkage(t *testing.T) {
	f := newFixture(t)
	provider := oauthprovider.New(t, oauthprovider.Options{})
	kept, removed := provider.Server("test-kept"), provider.Server("test-removed")
	before := f.authority(t, kept, removed)
	link(t, before, kept.ServerID)
	f.changed(t, "a link")
	link(t, before, removed.ServerID)
	f.changed(t, "a link")
	if _, err := before.Start(t.Context(), removed.ServerID, nil, operator); err != nil {
		t.Fatal(err)
	}

	// With no server configured, nothing is removed.
	if err := f.authority(t).RemoveUnconfigured(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := f.authority(t, kept).RemoveUnconfigured(t.Context()); err != nil {
		t.Fatal(err)
	}
	f.changed(t, "the removal")
	// The kept server's link left one row in each table.
	for _, table := range []string{"mcp_server_linkage", "mcp_oauth_token_state", "mcp_linkage_flow"} {
		var ofKept, all int
		if err := f.db.QueryRowContext(t.Context(), "SELECT count(*) FILTER (WHERE server_id = $1), count(*) FROM "+table,
			kept.ServerID).Scan(&ofKept, &all); err != nil {
			t.Fatal(err)
		}
		if ofKept != 1 || all != 1 {
			t.Errorf("%s holds %d rows of the kept server and %d in all", table, ofKept, all)
		}
	}
}
