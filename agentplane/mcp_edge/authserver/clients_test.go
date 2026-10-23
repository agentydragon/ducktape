package authserver

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"code.dny.dev/ssrf"
)

// wantRendered is a refusal the authorization endpoint renders, since it cannot trust the redirect URI.
func wantRendered(t *testing.T, what string, response *http.Response, body, code string) {
	t.Helper()
	var refusal registrationError
	if response.StatusCode != http.StatusBadRequest || response.Header.Get("Location") != "" ||
		json.Unmarshal([]byte(body), &refusal) != nil || refusal.Code != code {
		t.Errorf("%s: %s %s, want %s rendered", what, response.Status, body, code)
	}
}

func TestAMetadataDocumentClientIsRefusedUnlessItsDocumentDescribesIt(t *testing.T) {
	h := start(t)
	base := h.documents.server.URL
	// serve answers the path with the status and the document of the client_id.
	serve := func(path string, status int, clientID string, change func(map[string]any)) string {
		body := document(t, clientID, change)
		h.documents.handle(path, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(status)
			_, _ = w.Write(body)
		}))
		return clientID
	}
	moved := base + "/test-moved.json"
	serve("/test-target.json", http.StatusOK, moved, nil)
	h.documents.handle("/test-moved.json", http.RedirectHandler(base+"/test-target.json", http.StatusFound))
	other := base + "/test-other.json"
	serve("/test-other.json", http.StatusOK, base+"/test-another-client.json", nil)
	padded := base + "/test-padded.json"
	h.documents.publish("/test-padded.json", append(document(t, padded, nil), strings.Repeat(" ", maxDocumentBytes)...))
	var plain *httptest.Server
	plain = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(document(t, plain.URL+"/test-plain.json", nil))
	}))
	t.Cleanup(plain.Close)
	// Each document describes the client it is fetched for, but for what is wrong with it.
	for what, clientID := range map[string]string{
		"a document served over http":    plain.URL + "/test-plain.json",
		"a client_id with no path":       serve("/", http.StatusOK, base+"/", nil),
		"a client_id with a dot segment": serve("/test/../test-dotted.json", http.StatusOK, base+"/test/../test-dotted.json", nil),
		"a client_id with a fragment": serve("/test-fragment.json", http.StatusOK,
			base+"/test-fragment.json#test-fragment", nil),
		"a client_id with credentials": serve("/test-credentials.json", http.StatusOK,
			strings.Replace(base, "https://", "https://test-user@", 1)+"/test-credentials.json", nil),
		"a document answered with an error": serve("/test-missing.json", http.StatusNotFound, base+"/test-missing.json", nil),
		"a document behind a redirect":      moved,
		"a document over the size limit":    padded,
		"a document that is not JSON":       h.documents.publish("/test-text.json", []byte("test: not JSON")),
		"a document naming another client":  other,
		"a confidential client": h.documentClient(t, "/test-confidential.json", func(d map[string]any) {
			d["token_endpoint_auth_method"] = "client_secret_basic"
		}),
		"a script redirect URI beside the requested one": h.documentClient(t, "/test-script.json", func(d map[string]any) {
			d["redirect_uris"] = []string{testRedirectURI, "javascript:alert(1)"}
		}),
		"a client without the code flow": h.documentClient(t, "/test-refresh-only.json", func(d map[string]any) {
			d["grant_types"] = []string{"refresh_token"}
		}),
	} {
		response, body := must2(h.load(h.authorizeURL(newRequest(clientID), nil)))(t)
		wantRendered(t, what, response, body, "invalid_client")
	}
	for what, clientID := range map[string]string{
		"a client its document describes": h.documentClient(t, "/test-client.json", nil),
		// Claude's document lists jwt-bearer too.
		"a client also listing a grant this server does not serve": h.documentClient(t, "/test-jwt-bearer.json",
			func(d map[string]any) {
				d["grant_types"] = []string{"authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer"}
			}),
	} {
		if location := must(h.follow(h.authorizeURL(newRequest(clientID), nil)))(t); !strings.HasPrefix(location, consentPrefix) {
			t.Errorf("%s was sent to %s, want consent", what, location)
		}
	}
}

func TestProductionDocumentFetchesReachOnlyPublicAddresses(t *testing.T) {
	h := start(t, func(c *Config) { c.DocumentClient = NewDocumentClient() })
	loopback := h.documentClient(t, "/test-client.json", nil)
	for _, clientID := range []string{loopback, "https://169.254.169.254/latest/meta-data"} {
		if _, err := h.server.client(t.Context(), clientID); !errors.Is(err, ssrf.ErrProhibitedIP) {
			t.Errorf("client %s: %v, want the dial refused", clientID, err)
		}
	}
	// The document is there: the test's own client fetches it.
	response := must(h.documents.server.Client().Get(loopback))(t)
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Errorf("fetching %s: %s", loopback, response.Status)
	}
}

func TestARegistrationIsRefusedMetadataTheServerCannotServe(t *testing.T) {
	h := start(t)
	withRedirect := func(change map[string]any) map[string]any {
		change["redirect_uris"] = []string{testRedirectURI}
		return change
	}
	for _, refused := range []struct {
		what     string
		metadata any
		code     string
	}{
		{"no redirect URI", map[string]any{"client_name": "Test DCR client"}, "invalid_redirect_uri"},
		{"a script redirect URI", map[string]any{"redirect_uris": []string{"javascript:alert(1)"}}, "invalid_redirect_uri"},
		{"an http redirect URI off loopback", map[string]any{"redirect_uris": []string{"http://test-client.example/callback"}}, "invalid_redirect_uri"},
		{"an https redirect URI with no host", map[string]any{"redirect_uris": []string{"https:///callback"}}, "invalid_redirect_uri"},
		{"a redirect URI with a fragment", map[string]any{"redirect_uris": []string{"https://test-client.example/callback#test"}}, "invalid_redirect_uri"},
		{"a relative redirect URI", map[string]any{"redirect_uris": []string{"/callback"}}, "invalid_redirect_uri"},
		{"only the client credentials grant", withRedirect(map[string]any{"grant_types": []string{"client_credentials"}}), "invalid_client_metadata"},
		{"the token response type", withRedirect(map[string]any{"response_types": []string{"token"}}), "invalid_client_metadata"},
		{"metadata that is no object", "test-not-metadata", "invalid_client_metadata"},
		{"metadata over the size limit", withRedirect(map[string]any{"client_name": strings.Repeat("x", maxDocumentBytes)}), "invalid_client_metadata"},
	} {
		if status, answer := h.register(t, refused.metadata); status != http.StatusBadRequest || answer["error"] != refused.code {
			t.Errorf("%s: %d %v, want %s", refused.what, status, answer, refused.code)
		}
	}
	var registered int
	if err := h.db.QueryRowContext(t.Context(), `SELECT count(*) FROM oauth_registered_client`).Scan(&registered); err != nil {
		t.Fatal(err)
	}
	if registered != 0 {
		t.Errorf("the refused registrations stored %d clients", registered)
	}
}

func TestARegistrationRegistersAPublicClientOfTheCodeFlow(t *testing.T) {
	h := start(t)
	// A code may be sent to https, to http on loopback, and to a native app's private-use scheme.
	status, answer := h.register(t, map[string]any{
		"redirect_uris": []string{
			"https://test-client.example/callback", "http://localhost:8080/callback", "test.client:/callback",
		},
		"token_endpoint_auth_method": "client_secret_post",
		"grant_types":                []string{"authorization_code", "client_credentials", "refresh_token"},
	})
	clientID, _ := answer["client_id"].(string)
	if status != http.StatusCreated || clientID == "" || answer["client_secret"] != nil ||
		answer["token_endpoint_auth_method"] != "none" ||
		!reflect.DeepEqual(answer["grant_types"], []any{"authorization_code", "refresh_token"}) {
		t.Fatalf("registration: %d %v", status, answer)
	}
	r := newRequest(clientID)
	r.redirectURI = "test.client:/callback"
	if location := must(h.follow(h.authorizeURL(r, nil)))(t); !strings.HasPrefix(location, consentPrefix) {
		t.Errorf("the registered client was sent to %s, want consent", location)
	}
}

func TestBrowserClientsMayCallTheEndpointsFromAnyOrigin(t *testing.T) {
	h := start(t)
	for _, path := range []string{metadataPath, resourceMetadataPath, tokenPath, revocationPath, registrationPath} {
		request := must(http.NewRequestWithContext(t.Context(), http.MethodOptions, h.url+path, nil))(t)
		request.Header.Set("Origin", "https://test-browser-client.example")
		request.Header.Set("Access-Control-Request-Method", http.MethodPost)
		response := must(h.browser.Do(request))(t)
		response.Body.Close()
		if response.StatusCode != http.StatusNoContent || response.Header.Get("Access-Control-Allow-Origin") != "*" {
			t.Errorf("a preflight of %s: %s %v", path, response.Status, response.Header)
		}
	}
}
