package kubeapiproxy

import (
	"bufio"
	"context"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/textproto"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type recordingAuthority struct {
	mu       sync.Mutex
	requests []AuthorizationRequest
	headers  []http.Header
	decision AuthorizationResponse
	status   int
	decide   func(int) (AuthorizationResponse, int)
}

func (a *recordingAuthority) ServeHTTP(w http.ResponseWriter, request *http.Request) {
	var body AuthorizationRequest
	if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	a.mu.Lock()
	a.requests = append(a.requests, body)
	a.headers = append(a.headers, request.Header.Clone())
	call := len(a.requests)
	decision := a.decision
	status := a.status
	decide := a.decide
	a.mu.Unlock()
	if decide != nil {
		decision, status = decide(call)
	}
	if status == 0 {
		status = http.StatusOK
	}
	writeJSON(w, status, decision)
}

func allowedDecision() AuthorizationResponse {
	return AuthorizationResponse{Allowed: true, DecisionID: "sar:test"}
}

type bearerTransport struct {
	base  http.RoundTripper
	token string
}

func (t bearerTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	clone := request.Clone(request.Context())
	clone.Header.Set("Authorization", "Bearer "+t.token)
	return t.base.RoundTrip(clone)
}

func newTestProxy(t *testing.T, authority http.Handler, upstream http.Handler, mutate func(*Config)) *httptest.Server {
	t.Helper()
	authorityServer := httptest.NewServer(authority)
	t.Cleanup(authorityServer.Close)
	upstreamServer := httptest.NewServer(upstream)
	t.Cleanup(upstreamServer.Close)
	authorityURL, _ := url.Parse(authorityServer.URL + "/api/internal/kubernetes/authorize")
	upstreamURL, _ := url.Parse(upstreamServer.URL)
	config := Config{
		AuthorizationURL:           authorityURL,
		AllowInsecureAuthorization: true,
		Upstream:                   upstreamURL,
		UpstreamTransport:          bearerTransport{base: http.DefaultTransport, token: "proxy-kubernetes-token"},
	}
	if mutate != nil {
		mutate(&config)
	}
	handler, err := NewHandler(config)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return server
}

func request(t *testing.T, client *http.Client, method string, rawURL string, token string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(method, rawURL, nil)
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func openUpgrade(t *testing.T, proxyURL string, method string, upgrade string, streamProtocol string, extraHeaders http.Header) (net.Conn, *bufio.Reader, http.Header) {
	t.Helper()
	target, err := url.Parse(proxyURL)
	if err != nil {
		t.Fatal(err)
	}
	connection, err := net.Dial("tcp", target.Host)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	streamProtocolHeader := ""
	if streamProtocol != "" {
		streamProtocolHeader = fmt.Sprintf("X-Stream-Protocol-Version: %s\r\n", streamProtocol)
	}
	_, err = fmt.Fprintf(
		connection,
		"%s %s HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer caller-secret\r\nConnection: Upgrade\r\nUpgrade: %s\r\n%sImpersonate-User: cluster-admin\r\n",
		method,
		target.RequestURI(),
		target.Host,
		upgrade,
		streamProtocolHeader,
	)
	if err != nil {
		t.Fatal(err)
	}
	for name, values := range extraHeaders {
		for _, value := range values {
			if _, err := fmt.Fprintf(connection, "%s: %s\r\n", name, value); err != nil {
				t.Fatal(err)
			}
		}
	}
	if _, err := fmt.Fprint(connection, "\r\n"); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(connection)
	status, err := reader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(status, " 101 ") {
		t.Fatalf("upgrade status = %q", strings.TrimSpace(status))
	}
	headers, err := textproto.NewReader(reader).ReadMIMEHeader()
	if err != nil {
		t.Fatal(err)
	}
	return connection, reader, http.Header(headers)
}

func waitForConnectionClose(t *testing.T, connection net.Conn, reader *bufio.Reader, timeout time.Duration) {
	t.Helper()
	if err := connection.SetReadDeadline(time.Now().Add(timeout)); err != nil {
		t.Fatal(err)
	}
	_, err := reader.ReadByte()
	if err == nil {
		t.Fatal("upgraded connection remained open and returned unexpected data")
	}
	if timeoutError, ok := err.(net.Error); ok && timeoutError.Timeout() {
		t.Fatal("upgraded connection remained open past its cancellation bound")
	}
}

// streamingUpstream writes one chunk and then holds the response open until its
// request context is cancelled, so a test observes exactly when the proxy ends a
// stream. Writing without a Content-Length is what makes the response chunked,
// which is also what makes ReverseProxy relay it without buffering.
func streamingUpstream(t *testing.T, contentType string, chunk string, disconnected chan<- struct{}) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		w.Header().Set("Content-Type", contentType)
		if _, err := io.WriteString(w, chunk); err != nil {
			t.Error(err)
			return
		}
		w.(http.Flusher).Flush()
		<-request.Context().Done()
		close(disconnected)
	})
}

// readStreamedChunk fails unless the chunk reaches the caller while the upstream
// response is still open, which is what distinguishes a relayed stream from a
// response buffered until upstream completion.
func readStreamedChunk(t *testing.T, body io.Reader, want string) {
	t.Helper()
	buffer := make([]byte, len(want))
	if _, err := io.ReadFull(body, buffer); err != nil {
		t.Fatalf("read streamed chunk: %v", err)
	}
	if string(buffer) != want {
		t.Fatalf("streamed chunk = %q, want %q", buffer, want)
	}
}

// waitForStreamEnd requires the stream to end within timeout and to end
// truncated. Cancellation happens after the response headers are sent, so a
// caller cannot receive a status code — an intact chunked body would mean the
// proxy ended the stream in an orderly way it has no way to perform.
func waitForStreamEnd(t *testing.T, body io.Reader, timeout time.Duration) {
	t.Helper()
	result := make(chan error, 1)
	go func() {
		_, err := io.Copy(io.Discard, body)
		result <- err
	}()
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("streamed response ended intact rather than truncated")
		}
	case <-time.After(timeout):
		t.Fatal("streamed response remained open past its cancellation bound")
	}
}

func followedLogRequest(t *testing.T, proxy *httptest.Server, query string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods/web/log?"+query, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer caller-secret")
	response, err := proxy.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = response.Body.Close() })
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	return response
}

func upgradeUpstream(t *testing.T, disconnected chan<- struct{}, expectedMethod string, expectedUpgrade string, expectedStreamProtocol string, expectedRequestHeaders http.Header, responseHeaders http.Header) http.Handler {
	t.Helper()
	return http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if got := request.Method; got != expectedMethod {
			t.Errorf("upstream method = %q, want %q", got, expectedMethod)
		}
		if got := request.Header.Get("Authorization"); got != "Bearer proxy-kubernetes-token" {
			t.Errorf("upstream authorization = %q", got)
		}
		if got := request.Header.Get("Upgrade"); got != expectedUpgrade {
			t.Errorf("upstream Upgrade = %q", got)
		}
		if !headerContainsToken(request.Header, "Connection", "upgrade") {
			t.Errorf("upstream Connection = %q", request.Header.Get("Connection"))
		}
		if got := request.Header.Get("X-Stream-Protocol-Version"); got != expectedStreamProtocol {
			t.Errorf("upstream stream protocol = %q", got)
		}
		if got := request.Header.Get("Impersonate-User"); got != "" {
			t.Errorf("upstream received impersonation header %q", got)
		}
		for name, values := range expectedRequestHeaders {
			if got := request.Header.Values(name); !reflect.DeepEqual(got, values) {
				t.Errorf("upstream %s = %#v, want %#v", name, got, values)
			}
		}
		hijacker, ok := w.(http.Hijacker)
		if !ok {
			t.Error("upstream response writer cannot hijack")
			return
		}
		connection, buffered, err := hijacker.Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		defer connection.Close()
		_, _ = fmt.Fprintf(buffered, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: %s\r\n", expectedUpgrade)
		if expectedStreamProtocol != "" {
			_, _ = fmt.Fprintf(buffered, "X-Stream-Protocol-Version: %s\r\n", expectedStreamProtocol)
		}
		for name, values := range responseHeaders {
			for _, value := range values {
				_, _ = fmt.Fprintf(buffered, "%s: %s\r\n", name, value)
			}
		}
		_, _ = fmt.Fprint(buffered, "\r\n")
		if err := buffered.Flush(); err != nil {
			t.Error(err)
			return
		}
		_, _ = io.Copy(io.Discard, buffered)
		close(disconnected)
	})
}

func TestAuthorizationContractUsesSnakeCaseJSON(t *testing.T) {
	body, err := json.Marshal(AuthorizationRequest{
		Attributes: RequestAttributes{
			ResourceRequest: true,
			APIGroup:        "apps",
			APIVersion:      "v1",
			FieldSelector:   "metadata.name=web",
			LabelSelector:   "app=web",
		},
		RequiredScope: GrantScope{Kind: grantScopeNamespaces, Namespaces: []string{"demo"}},
		RequiredRules: []PolicyRule{{
			APIGroups:       []string{"apps"},
			Resources:       []string{"deployments"},
			ResourceNames:   []string{"web"},
			NonResourceURLs: []string{"/version"},
			Verbs:           []string{"get"},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	validUntil := time.Unix(1, 0).UTC()
	decisionBody, err := json.Marshal(AuthorizationResponse{
		Allowed: true, DecisionID: "sar:test", ValidUntil: &validUntil,
	})
	if err != nil {
		t.Fatal(err)
	}
	encoded := string(body) + string(decisionBody)
	for _, field := range []string{
		"resource_request",
		"api_group",
		"api_version",
		"field_selector",
		"label_selector",
		"required_scope",
		"required_rules",
		"api_groups",
		"resources",
		"resource_names",
		"non_resource_urls",
		"decision_id",
		"valid_until",
	} {
		if !strings.Contains(encoded, `"`+field+`"`) {
			t.Errorf("JSON does not contain %q: %s", field, encoded)
		}
	}
}

func TestNamedPodLogRequestIsAuthorizedAndForwarded(t *testing.T) {
	decision := allowedDecision()
	authority := &recordingAuthority{decision: decision}
	upstream := http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if got := request.Header.Get("Authorization"); got != "Bearer proxy-kubernetes-token" {
			t.Errorf("upstream authorization = %q", got)
		}
		for _, name := range []string{"Impersonate-User", "X-Remote-User", "Cookie", "Proxy-Authorization", "X-Api-Key", "X-Forwarded-For"} {
			if got := request.Header.Get(name); got != "" {
				t.Errorf("upstream received %s: %q", name, got)
			}
		}
		if got := request.Header.Get("Accept"); got != "application/json" {
			t.Errorf("upstream Accept = %q", got)
		}
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("logs"))
	})
	proxy := newTestProxy(t, authority, upstream, nil)

	req, err := http.NewRequest(http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods/web/log?tailLines=25", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer caller-secret")
	req.Header.Set("Impersonate-User", "cluster-admin")
	req.Header.Set("X-Remote-User", "cluster-admin")
	req.Header.Set("Cookie", "console_session=secret")
	req.Header.Set("Proxy-Authorization", "Basic secret")
	req.Header.Set("X-Api-Key", "secret")
	req.Header.Set("X-Forwarded-For", "127.0.0.1")
	req.Header.Set("Accept", "application/json")
	response, err := proxy.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if got := response.Header.Get("X-Haku-Kubernetes-Decision-ID"); got != decision.DecisionID {
		t.Errorf("decision header = %q, want %q", got, decision.DecisionID)
	}

	authority.mu.Lock()
	defer authority.mu.Unlock()
	if len(authority.requests) != 1 {
		t.Fatalf("authorization requests = %d", len(authority.requests))
	}
	got := authority.requests[0]
	wantAttributes := RequestAttributes{
		ResourceRequest: true,
		Verb:            "get",
		APIVersion:      "v1",
		Namespace:       "demo",
		Resource:        "pods",
		Subresource:     "log",
		Name:            "web",
		Path:            "/api/v1/namespaces/demo/pods/web/log",
	}
	if got.Attributes != wantAttributes {
		t.Errorf("attributes = %#v, want %#v", got.Attributes, wantAttributes)
	}
	if got.RequiredScope.Kind != grantScopeNamespaces || strings.Join(got.RequiredScope.Namespaces, ",") != "demo" {
		t.Errorf("scope = %#v", got.RequiredScope)
	}
	if len(got.RequiredRules) != 1 {
		t.Fatalf("rules = %#v", got.RequiredRules)
	}
	rule := got.RequiredRules[0]
	if strings.Join(rule.APIGroups, ",") != "" || strings.Join(rule.Resources, ",") != "pods/log" || strings.Join(rule.Verbs, ",") != "get" || strings.Join(rule.ResourceNames, ",") != "web" {
		t.Errorf("rule = %#v", rule)
	}
	if got := authority.headers[0].Get("Authorization"); got != "Bearer caller-secret" {
		t.Errorf("authority authorization = %q", got)
	}
}

func TestListRequestProducesListRule(t *testing.T) {
	authority := &recordingAuthority{decision: allowedDecision()}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"kind": "PodList"})
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods?labelSelector=app%3Dweb", "caller")
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	authority.mu.Lock()
	defer authority.mu.Unlock()
	got := authority.requests[0]
	if got.Attributes.Verb != "list" || got.Attributes.LabelSelector != "app=web" {
		t.Errorf("attributes = %#v", got.Attributes)
	}
	if len(got.RequiredRules[0].ResourceNames) != 0 {
		t.Errorf("list rule unexpectedly has resourceNames: %#v", got.RequiredRules[0])
	}
}

func TestNameFieldSelectorUsesKubernetesResourceName(t *testing.T) {
	authority := &recordingAuthority{decision: allowedDecision()}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"kind": "PodList"})
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods?fieldSelector=metadata.name%3Dweb", "caller")
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	authority.mu.Lock()
	defer authority.mu.Unlock()
	got := authority.requests[0]
	if got.Attributes.Verb != "list" || got.Attributes.Name != "web" || got.Attributes.FieldSelector != "metadata.name=web" {
		t.Errorf("attributes = %#v", got.Attributes)
	}
	if strings.Join(got.RequiredRules[0].ResourceNames, ",") != "web" {
		t.Errorf("field-selected list rule = %#v", got.RequiredRules[0])
	}
}

func TestNonResourceRequestProducesNonResourceRule(t *testing.T) {
	authority := &recordingAuthority{decision: allowedDecision()}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"gitVersion": "test"})
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/version", "caller")
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
	authority.mu.Lock()
	defer authority.mu.Unlock()
	rule := authority.requests[0].RequiredRules[0]
	if scope := authority.requests[0].RequiredScope; scope.Kind != grantScopeNonResource || len(scope.Namespaces) != 0 {
		t.Errorf("scope = %#v", scope)
	}
	if strings.Join(rule.NonResourceURLs, ",") != "/version" || strings.Join(rule.Verbs, ",") != "get" {
		t.Errorf("rule = %#v", rule)
	}
}

func TestUnnamespacedResourceScopeComesFromDiscovery(t *testing.T) {
	for _, test := range []struct {
		name       string
		path       string
		namespaced bool
		wantKind   string
	}{
		{name: "all namespaces", path: "/api/v1/pods", namespaced: true, wantKind: grantScopeAllNamespaces},
		{name: "cluster resource", path: "/api/v1/nodes", namespaced: false, wantKind: grantScopeCluster},
	} {
		t.Run(test.name, func(t *testing.T) {
			authority := &recordingAuthority{decision: allowedDecision()}
			resolver := &staticResourceScopes{namespaced: test.namespaced}
			proxy := newTestProxy(t, authority, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				writeJSON(w, http.StatusOK, map[string]string{"kind": "List"})
			}), func(config *Config) {
				config.ResourceScopes = resolver
			})
			response := request(t, proxy.Client(), http.MethodGet, proxy.URL+test.path, "caller")
			defer response.Body.Close()
			if response.StatusCode != http.StatusOK {
				t.Fatalf("status = %d", response.StatusCode)
			}
			authority.mu.Lock()
			defer authority.mu.Unlock()
			if scope := authority.requests[0].RequiredScope; scope.Kind != test.wantKind || len(scope.Namespaces) != 0 {
				t.Errorf("scope = %#v", scope)
			}
			if len(resolver.calls) != 1 {
				t.Fatalf("scope resolver calls = %#v", resolver.calls)
			}
		})
	}
}

func TestMissingBearerIsRejectedBeforeAuthority(t *testing.T) {
	authority := &recordingAuthority{decision: allowedDecision()}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream called")
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/api/v1/pods", "")
	defer response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if len(authority.requests) != 0 {
		t.Fatalf("authority called %d times", len(authority.requests))
	}
}

func TestDeniedRequestIsNotForwarded(t *testing.T) {
	authority := &recordingAuthority{decision: AuthorizationResponse{Allowed: false, Reason: "standing policy denied secrets"}}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream called")
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/secrets", "caller")
	defer response.Body.Close()
	if response.StatusCode != http.StatusForbidden {
		t.Fatalf("status = %d", response.StatusCode)
	}
}

func TestAuthorityFailureFailsClosed(t *testing.T) {
	authority := &recordingAuthority{status: http.StatusNotImplemented}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream called")
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods", "caller")
	defer response.Body.Close()
	if response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d", response.StatusCode)
	}
}

func TestAllowedDecisionRequiresDecisionIdentity(t *testing.T) {
	authority := &recordingAuthority{decision: AuthorizationResponse{Allowed: true}}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream called")
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods", "caller")
	response.Body.Close()
	if response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d", response.StatusCode)
	}
}

func TestAuthorityRedirectDoesNotForwardCallerBearer(t *testing.T) {
	var redirectTargetCalled atomic.Bool
	redirectTarget := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		redirectTargetCalled.Store(true)
	}))
	t.Cleanup(redirectTarget.Close)
	authority := http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		http.Redirect(w, request, redirectTarget.URL, http.StatusTemporaryRedirect)
	})
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream called")
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods", "caller")
	response.Body.Close()
	if response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if redirectTargetCalled.Load() {
		t.Fatal("authorization client followed a redirect with the caller credential")
	}
}

func TestRequestContextEndsAtProxyRequestTimeout(t *testing.T) {
	authority := &recordingAuthority{decision: allowedDecision()}
	upstreamDone := make(chan struct{})
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		<-request.Context().Done()
		close(upstreamDone)
	}), func(config *Config) {
		config.RequestTimeout = 100 * time.Millisecond
	})

	req, _ := http.NewRequestWithContext(context.Background(), http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods/web/log", nil)
	req.Header.Set("Authorization", "Bearer caller")
	_, _ = proxy.Client().Do(req)
	select {
	case <-upstreamDone:
	case <-time.After(time.Second):
		t.Fatal("upstream request survived proxy timeout")
	}
}

func TestRequestContextEndsAtTemporaryDecisionExpiry(t *testing.T) {
	validUntil := time.Now().Add(100 * time.Millisecond)
	authority := &recordingAuthority{decision: AuthorizationResponse{
		Allowed: true, DecisionID: "grant:test", ValidUntil: &validUntil,
	}}
	upstreamDone := make(chan struct{})
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		<-request.Context().Done()
		close(upstreamDone)
	}), func(config *Config) {
		config.RequestTimeout = 5 * time.Second
	})

	req, _ := http.NewRequestWithContext(
		context.Background(), http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods/web/log", nil,
	)
	req.Header.Set("Authorization", "Bearer caller")
	_, _ = proxy.Client().Do(req)
	select {
	case <-upstreamDone:
	case <-time.After(time.Second):
		t.Fatal("upstream request survived temporary authorization expiry")
	}
}

func TestExpiredTemporaryDecisionIsNotForwarded(t *testing.T) {
	validUntil := time.Now().Add(-time.Second)
	authority := &recordingAuthority{decision: AuthorizationResponse{
		Allowed: true, DecisionID: "grant:expired", ValidUntil: &validUntil,
	}}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream called")
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods", "caller")
	response.Body.Close()
	if response.StatusCode != http.StatusForbidden {
		t.Fatalf("status = %d", response.StatusCode)
	}
}

func TestExecUpgradeUsesKubernetesAuthorizationAndHardExpiry(t *testing.T) {
	validUntil := time.Now().Add(250 * time.Millisecond)
	authority := &recordingAuthority{decision: AuthorizationResponse{
		Allowed: true, DecisionID: "grant:exec", ValidUntil: &validUntil,
	}}
	upstreamDisconnected := make(chan struct{})
	proxy := newTestProxy(t, authority, upgradeUpstream(t, upstreamDisconnected, http.MethodPost, "test-stream", "v5.channel.k8s.io", nil, nil), func(config *Config) {
		config.RequestTimeout = 20 * time.Millisecond
		config.StreamRevalidationInterval = time.Hour
	})

	started := time.Now()
	connection, reader, headers := openUpgrade(
		t,
		proxy.URL+"/api/v1/namespaces/demo/pods/web/exec?command=%2Fbin%2Ftrue&stdout=true&stderr=true&stdin=false&tty=false",
		http.MethodPost,
		"test-stream",
		"v5.channel.k8s.io",
		nil,
	)
	if got := headers.Get("X-Haku-Kubernetes-Decision-ID"); got != "grant:exec" {
		t.Errorf("decision header = %q", got)
	}
	waitForConnectionClose(t, connection, reader, time.Second)
	if elapsed := time.Since(started); elapsed < 100*time.Millisecond {
		t.Fatalf("exec ended after %s; ordinary request timeout incorrectly bounded the stream", elapsed)
	}
	select {
	case <-upstreamDisconnected:
	case <-time.After(time.Second):
		t.Fatal("upstream upgraded connection survived grant expiry")
	}

	authority.mu.Lock()
	defer authority.mu.Unlock()
	if len(authority.requests) != 1 {
		t.Fatalf("authorization requests = %d", len(authority.requests))
	}
	got := authority.requests[0]
	if got.Attributes.Verb != "create" || got.Attributes.Namespace != "demo" || got.Attributes.Resource != "pods" || got.Attributes.Subresource != "exec" || got.Attributes.Name != "web" {
		t.Errorf("attributes = %#v", got.Attributes)
	}
	rule := got.RequiredRules[0]
	if strings.Join(rule.Resources, ",") != "pods/exec" || strings.Join(rule.Verbs, ",") != "create" || strings.Join(rule.ResourceNames, ",") != "web" {
		t.Errorf("rule = %#v", rule)
	}
}

func TestExecUpgradeUsesHTTP1ForHTTP2CapableKubernetesUpstream(t *testing.T) {
	validUntil := time.Now().Add(250 * time.Millisecond)
	authority := &recordingAuthority{decision: AuthorizationResponse{
		Allowed: true, DecisionID: "grant:exec", ValidUntil: &validUntil,
	}}
	upstreamDisconnected := make(chan struct{})
	execUpstream := upgradeUpstream(t, upstreamDisconnected, http.MethodPost, "test-stream", "v5.channel.k8s.io", nil, nil)
	upstreamServer := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.ProtoMajor != 1 {
			t.Errorf("upstream protocol = %s, want HTTP/1.1 for upgrade", request.Proto)
		}
		execUpstream.ServeHTTP(w, request)
	}))
	upstreamServer.EnableHTTP2 = true
	upstreamServer.StartTLS()
	t.Cleanup(upstreamServer.Close)

	serviceAccountDirectory := t.TempDir()
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: upstreamServer.Certificate().Raw})
	if err := os.WriteFile(filepath.Join(serviceAccountDirectory, "ca.crt"), caPEM, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(serviceAccountDirectory, "token"), []byte("proxy-kubernetes-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	serverURL, _ := url.Parse(upstreamServer.URL)
	serviceHost, servicePort, err := net.SplitHostPort(serverURL.Host)
	if err != nil {
		t.Fatal(err)
	}
	upstreamURL, upstreamTransport, err := InClusterUpstream(InClusterConfig{
		ServiceHost:             serviceHost,
		ServicePort:             servicePort,
		ServiceAccountDirectory: serviceAccountDirectory,
	})
	if err != nil {
		t.Fatal(err)
	}
	authorityServer := httptest.NewServer(authority)
	t.Cleanup(authorityServer.Close)
	authorityURL, _ := url.Parse(authorityServer.URL + "/api/internal/kubernetes/authorize")
	handler, err := NewHandler(Config{
		AuthorizationURL:           authorityURL,
		AllowInsecureAuthorization: true,
		Upstream:                   upstreamURL,
		UpstreamTransport:          upstreamTransport,
		RequestTimeout:             20 * time.Millisecond,
		StreamRevalidationInterval: time.Hour,
	})
	if err != nil {
		t.Fatal(err)
	}
	proxy := httptest.NewServer(handler)
	t.Cleanup(proxy.Close)

	connection, reader, _ := openUpgrade(
		t,
		proxy.URL+"/api/v1/namespaces/demo/pods/web/exec?command=%2Fbin%2Ftrue&stdout=true&stderr=true&stdin=false&tty=false",
		http.MethodPost,
		"test-stream",
		"v5.channel.k8s.io",
		nil,
	)
	waitForConnectionClose(t, connection, reader, time.Second)
	select {
	case <-upstreamDisconnected:
	case <-time.After(time.Second):
		t.Fatal("HTTP/1.1 upstream connection survived grant expiry")
	}
}

func TestExecUpgradeRevalidatesAndClosesFailClosed(t *testing.T) {
	for _, test := range []struct {
		name       string
		revalidate func() (AuthorizationResponse, int)
	}{
		{
			name: "revoked",
			revalidate: func() (AuthorizationResponse, int) {
				return AuthorizationResponse{Allowed: false, Reason: "grant revoked"}, http.StatusOK
			},
		},
		{
			name: "authority unavailable",
			revalidate: func() (AuthorizationResponse, int) {
				return AuthorizationResponse{}, http.StatusServiceUnavailable
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			validUntil := time.Now().Add(5 * time.Second)
			authority := &recordingAuthority{decide: func(call int) (AuthorizationResponse, int) {
				if call == 1 {
					return AuthorizationResponse{Allowed: true, DecisionID: "grant:exec", ValidUntil: &validUntil}, http.StatusOK
				}
				return test.revalidate()
			}}
			upstreamDisconnected := make(chan struct{})
			proxy := newTestProxy(t, authority, upgradeUpstream(t, upstreamDisconnected, http.MethodPost, "test-stream", "v5.channel.k8s.io", nil, nil), func(config *Config) {
				config.AuthorizationTimeout = 100 * time.Millisecond
				config.StreamRevalidationInterval = 25 * time.Millisecond
			})

			connection, reader, _ := openUpgrade(
				t,
				proxy.URL+"/api/v1/namespaces/demo/pods/web/exec?command=%2Fbin%2Ftrue&stdout=true&stderr=true&stdin=false&tty=false",
				http.MethodPost,
				"test-stream",
				"v5.channel.k8s.io",
				nil,
			)
			waitForConnectionClose(t, connection, reader, time.Second)
			select {
			case <-upstreamDisconnected:
			case <-time.After(time.Second):
				t.Fatal("upstream upgraded connection survived failed revalidation")
			}
			authority.mu.Lock()
			defer authority.mu.Unlock()
			if len(authority.requests) < 2 {
				t.Fatalf("authorization requests = %d, want initial plus revalidation", len(authority.requests))
			}
			if !reflect.DeepEqual(authority.requests[0], authority.requests[1]) {
				t.Errorf("revalidation request changed: first=%#v second=%#v", authority.requests[0], authority.requests[1])
			}
		})
	}
}

func TestPortForwardUpgradeIsAuthorizedAndClosesAfterRevocation(t *testing.T) {
	validUntil := time.Now().Add(5 * time.Second)
	authority := &recordingAuthority{decide: func(call int) (AuthorizationResponse, int) {
		if call == 1 {
			return AuthorizationResponse{Allowed: true, DecisionID: "grant:portforward", ValidUntil: &validUntil}, http.StatusOK
		}
		return AuthorizationResponse{Allowed: false, Reason: "grant withdrawn"}, http.StatusOK
	}}
	upstreamDisconnected := make(chan struct{})
	proxy := newTestProxy(t, authority, upgradeUpstream(t, upstreamDisconnected, http.MethodPost, "SPDY/3.1", "portforward.k8s.io", nil, nil), func(config *Config) {
		config.AuthorizationTimeout = 100 * time.Millisecond
		config.StreamRevalidationInterval = 25 * time.Millisecond
	})

	connection, reader, headers := openUpgrade(
		t,
		proxy.URL+"/api/v1/namespaces/demo/pods/web/portforward?ports=5432",
		http.MethodPost,
		"SPDY/3.1",
		"portforward.k8s.io",
		nil,
	)
	if got := headers.Get("X-Haku-Kubernetes-Decision-ID"); got != "grant:portforward" {
		t.Errorf("decision header = %q", got)
	}
	waitForConnectionClose(t, connection, reader, time.Second)
	select {
	case <-upstreamDisconnected:
	case <-time.After(time.Second):
		t.Fatal("port-forward connection survived failed revalidation")
	}

	authority.mu.Lock()
	defer authority.mu.Unlock()
	if len(authority.requests) < 2 {
		t.Fatalf("authorization requests = %d, want initial plus revalidation", len(authority.requests))
	}
	initial := authority.requests[0]
	if initial.Attributes.Verb != "create" || initial.Attributes.Namespace != "demo" || initial.Attributes.Resource != "pods" || initial.Attributes.Subresource != "portforward" || initial.Attributes.Name != "web" {
		t.Errorf("attributes = %#v", initial.Attributes)
	}
	rule := initial.RequiredRules[0]
	if strings.Join(rule.Resources, ",") != "pods/portforward" || strings.Join(rule.Verbs, ",") != "create" || strings.Join(rule.ResourceNames, ",") != "web" {
		t.Errorf("rule = %#v", rule)
	}
	if !reflect.DeepEqual(initial, authority.requests[1]) {
		t.Errorf("revalidation request changed: first=%#v second=%#v", initial, authority.requests[1])
	}
}

func TestPortForwardUpgradeUsesHardExpiry(t *testing.T) {
	validUntil := time.Now().Add(250 * time.Millisecond)
	authority := &recordingAuthority{decision: AuthorizationResponse{
		Allowed: true, DecisionID: "grant:portforward", ValidUntil: &validUntil,
	}}
	upstreamDisconnected := make(chan struct{})
	proxy := newTestProxy(t, authority, upgradeUpstream(t, upstreamDisconnected, http.MethodPost, "SPDY/3.1", "portforward.k8s.io", nil, nil), func(config *Config) {
		config.RequestTimeout = 20 * time.Millisecond
		config.StreamRevalidationInterval = time.Hour
	})

	started := time.Now()
	connection, reader, _ := openUpgrade(
		t,
		proxy.URL+"/api/v1/namespaces/demo/pods/web/portforward?ports=5432",
		http.MethodPost,
		"SPDY/3.1",
		"portforward.k8s.io",
		nil,
	)
	waitForConnectionClose(t, connection, reader, time.Second)
	if elapsed := time.Since(started); elapsed < 100*time.Millisecond {
		t.Fatalf("port-forward ended after %s; ordinary request timeout incorrectly bounded the stream", elapsed)
	}
	select {
	case <-upstreamDisconnected:
	case <-time.After(time.Second):
		t.Fatal("upstream port-forward connection survived grant expiry")
	}
}

func TestPortForwardWebSocketHeadersAreForwarded(t *testing.T) {
	const webSocketAccept = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
	requestHeaders := http.Header{
		"Sec-WebSocket-Key":      {"dGhlIHNhbXBsZSBub25jZQ=="},
		"Sec-WebSocket-Protocol": {"SPDY/3.1+portforward.k8s.io"},
		"Sec-WebSocket-Version":  {"13"},
	}
	responseHeaders := http.Header{
		"Sec-Websocket-Accept": {webSocketAccept},
	}
	authority := &recordingAuthority{decision: allowedDecision()}
	upstreamDisconnected := make(chan struct{})
	proxy := newTestProxy(t, authority, upgradeUpstream(t, upstreamDisconnected, http.MethodGet, "websocket", "", requestHeaders, responseHeaders), nil)

	connection, reader, headers := openUpgrade(
		t,
		proxy.URL+"/api/v1/namespaces/demo/pods/web/portforward?ports=5432",
		http.MethodGet,
		"websocket",
		"",
		requestHeaders,
	)
	if got := headers.Get("Sec-WebSocket-Accept"); got != webSocketAccept {
		t.Errorf("Sec-WebSocket-Accept = %q", got)
	}
	if err := connection.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-upstreamDisconnected:
	case <-time.After(time.Second):
		t.Fatal("upstream WebSocket connection did not close with the client")
	}
	if reader.Buffered() != 0 {
		t.Errorf("unexpected buffered WebSocket response data: %d bytes", reader.Buffered())
	}
	authority.mu.Lock()
	defer authority.mu.Unlock()
	if len(authority.requests) != 1 {
		t.Fatalf("authorization requests = %d, want 1", len(authority.requests))
	}
	request := authority.requests[0]
	if request.Attributes.Verb != "create" {
		t.Errorf("WebSocket port-forward authorization verb = %q, want create", request.Attributes.Verb)
	}
	if rule := request.RequiredRules[0]; strings.Join(rule.Resources, ",") != "pods/portforward" || strings.Join(rule.Verbs, ",") != "create" {
		t.Errorf("WebSocket port-forward rule = %#v", rule)
	}
}

func TestFollowedPodLogIsAuthorizedAsAnOrdinaryLogReadAndStreamed(t *testing.T) {
	decision := allowedDecision()
	authority := &recordingAuthority{decision: decision}
	upstreamDisconnected := make(chan struct{})
	proxy := newTestProxy(t, authority, streamingUpstream(t, "text/plain", "first line\n", upstreamDisconnected), nil)

	response := followedLogRequest(t, proxy, "follow=true&tailLines=25")
	readStreamedChunk(t, response.Body, "first line\n")
	if got := response.Header.Get("X-Haku-Kubernetes-Decision-ID"); got != decision.DecisionID {
		t.Errorf("decision header = %q, want %q", got, decision.DecisionID)
	}

	authority.mu.Lock()
	requests := append([]AuthorizationRequest(nil), authority.requests...)
	authority.mu.Unlock()
	if len(requests) != 1 {
		t.Fatalf("authorization requests = %d", len(requests))
	}
	// Following is not a distinct Kubernetes RBAC attribute, so a followed log
	// must be authorized as exactly the same request as a bounded one.
	wantAttributes := RequestAttributes{
		ResourceRequest: true,
		Verb:            "get",
		APIVersion:      "v1",
		Namespace:       "demo",
		Resource:        "pods",
		Subresource:     "log",
		Name:            "web",
		Path:            "/api/v1/namespaces/demo/pods/web/log",
	}
	if requests[0].Attributes != wantAttributes {
		t.Errorf("attributes = %#v, want %#v", requests[0].Attributes, wantAttributes)
	}
	if requests[0].RequiredScope.Kind != grantScopeNamespaces || strings.Join(requests[0].RequiredScope.Namespaces, ",") != "demo" {
		t.Errorf("scope = %#v", requests[0].RequiredScope)
	}
	rule := requests[0].RequiredRules[0]
	if strings.Join(rule.Resources, ",") != "pods/log" || strings.Join(rule.Verbs, ",") != "get" || strings.Join(rule.ResourceNames, ",") != "web" {
		t.Errorf("rule = %#v", rule)
	}
}

func TestFollowedPodLogOutlivesTheOrdinaryRequestTimeout(t *testing.T) {
	authority := &recordingAuthority{decision: allowedDecision()}
	upstreamDisconnected := make(chan struct{})
	proxy := newTestProxy(t, authority, streamingUpstream(t, "text/plain", "line\n", upstreamDisconnected), func(config *Config) {
		config.RequestTimeout = 100 * time.Millisecond
		config.StreamRevalidationInterval = time.Hour
	})

	response := followedLogRequest(t, proxy, "follow=true")
	readStreamedChunk(t, response.Body, "line\n")
	// A standing decision carries no valid_until, so nothing bounds this stream
	// but revalidation. The ordinary request timeout must not apply to it.
	select {
	case <-upstreamDisconnected:
		t.Fatal("followed log was cut short by the ordinary request timeout")
	case <-time.After(400 * time.Millisecond):
	}
}

func TestFollowedPodLogEndsAtTemporaryDecisionExpiry(t *testing.T) {
	validUntil := time.Now().Add(250 * time.Millisecond)
	authority := &recordingAuthority{decision: AuthorizationResponse{
		Allowed: true, DecisionID: "grant:log", ValidUntil: &validUntil,
	}}
	upstreamDisconnected := make(chan struct{})
	proxy := newTestProxy(t, authority, streamingUpstream(t, "text/plain", "line\n", upstreamDisconnected), func(config *Config) {
		config.RequestTimeout = time.Hour
		config.StreamRevalidationInterval = time.Hour
	})

	response := followedLogRequest(t, proxy, "follow=true")
	readStreamedChunk(t, response.Body, "line\n")
	waitForStreamEnd(t, response.Body, 2*time.Second)
	select {
	case <-upstreamDisconnected:
	case <-time.After(time.Second):
		t.Fatal("upstream log stream survived temporary authorization expiry")
	}
}

func TestFollowedPodLogRevalidatesAndClosesFailClosed(t *testing.T) {
	for _, test := range []struct {
		name       string
		revalidate func() (AuthorizationResponse, int)
	}{
		{
			name: "revoked",
			revalidate: func() (AuthorizationResponse, int) {
				return AuthorizationResponse{Allowed: false, Reason: "grant revoked"}, http.StatusOK
			},
		},
		{
			name: "authority unavailable",
			revalidate: func() (AuthorizationResponse, int) {
				return AuthorizationResponse{}, http.StatusServiceUnavailable
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			validUntil := time.Now().Add(5 * time.Second)
			authority := &recordingAuthority{decide: func(call int) (AuthorizationResponse, int) {
				if call == 1 {
					return AuthorizationResponse{Allowed: true, DecisionID: "grant:log", ValidUntil: &validUntil}, http.StatusOK
				}
				return test.revalidate()
			}}
			upstreamDisconnected := make(chan struct{})
			proxy := newTestProxy(t, authority, streamingUpstream(t, "text/plain", "line\n", upstreamDisconnected), func(config *Config) {
				config.AuthorizationTimeout = 100 * time.Millisecond
				config.StreamRevalidationInterval = 25 * time.Millisecond
			})

			response := followedLogRequest(t, proxy, "follow=true")
			readStreamedChunk(t, response.Body, "line\n")
			waitForStreamEnd(t, response.Body, 2*time.Second)
			select {
			case <-upstreamDisconnected:
			case <-time.After(time.Second):
				t.Fatal("upstream log stream survived failed revalidation")
			}

			authority.mu.Lock()
			defer authority.mu.Unlock()
			if len(authority.requests) < 2 {
				t.Fatalf("authorization requests = %d, want initial plus revalidation", len(authority.requests))
			}
			if !reflect.DeepEqual(authority.requests[0], authority.requests[1]) {
				t.Errorf("revalidation request changed: first=%#v second=%#v", authority.requests[0], authority.requests[1])
			}
		})
	}
}

// A followed log is classified by apimachinery's boolean conversion, so the
// proxy cannot decide a request is bounded that kube-apiserver will stream.
func TestFollowedPodLogClassificationMatchesKubernetesBooleanParameters(t *testing.T) {
	for _, test := range []struct {
		query  string
		follow bool
	}{
		{query: "", follow: false},
		{query: "follow=false", follow: false},
		{query: "follow=FALSE", follow: false},
		{query: "follow=0", follow: false},
		{query: "follow=true", follow: true},
		{query: "follow=1", follow: true},
		{query: "follow=T", follow: true},
		// An empty or unparseable value is true, not an error and not false.
		{query: "follow=", follow: true},
		{query: "follow=not-a-boolean", follow: true},
		// Whitespace is significant, so a padded "false" is true.
		{query: "follow=%20false", follow: true},
		// A repeated parameter is decided by its first value alone.
		{query: "follow=false&follow=true", follow: false},
		{query: "follow=true&follow=false", follow: true},
	} {
		t.Run(test.query, func(t *testing.T) {
			request, err := http.NewRequest(http.MethodGet, "https://kubernetes/api/v1/namespaces/demo/pods/web/log?"+test.query, nil)
			if err != nil {
				t.Fatal(err)
			}
			attributes := RequestAttributes{Resource: "pods", Subresource: "log", Name: "web", Verb: "get"}
			if got := isFollowLogRequest(attributes, request); got != test.follow {
				t.Errorf("isFollowLogRequest = %t, want %t", got, test.follow)
			}
		})
	}
}

func TestUnsupportedLongLivedAndInteractiveRequests(t *testing.T) {
	authority := &recordingAuthority{decision: allowedDecision()}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream called")
	}), nil)

	requests := []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/api/v1/namespaces/demo/pods?watch=true"},
		{http.MethodGet, "/api/v1/namespaces/demo/pods?watch="},
		{http.MethodGet, "/api/v1/namespaces/demo/pods?watch=not-a-boolean"},
		{http.MethodGet, "/api/v1/namespaces/demo/pods?watch=false&watch=true"},
		{http.MethodGet, "/api/v1/namespaces/demo/pods/web/exec"},
		{http.MethodGet, "/api/v1/namespaces/demo/pods/web/attach"},
		{http.MethodPost, "/api/v1/namespaces/demo/pods/web/portforward?ports=5432"},
		{http.MethodGet, "/api/v1/namespaces/demo/pods/web/proxy/path"},
	}
	for _, requestCase := range requests {
		response := request(t, proxy.Client(), requestCase.method, proxy.URL+requestCase.path, "caller")
		response.Body.Close()
		if response.StatusCode != http.StatusNotImplemented {
			t.Errorf("%s %s status = %d", requestCase.method, requestCase.path, response.StatusCode)
		}
	}
	if len(authority.requests) != 0 {
		t.Fatalf("authority called for unsupported request")
	}

	req, _ := http.NewRequest(http.MethodGet, proxy.URL+"/version", nil)
	req.Header.Set("Authorization", "Bearer caller")
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", "test-stream")
	response, err := proxy.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusNotImplemented {
		t.Fatalf("non-exec upgrade status = %d", response.StatusCode)
	}

	req, _ = http.NewRequest(http.MethodGet, proxy.URL+"/api/v1/namespaces/demo/pods/web/portforward?ports=5432", nil)
	req.Header.Set("Authorization", "Bearer caller")
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", "test-stream")
	response, err = proxy.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusNotImplemented {
		t.Fatalf("non-create port-forward upgrade status = %d", response.StatusCode)
	}
	if len(authority.requests) != 0 {
		t.Fatal("authority called for unsupported request")
	}
}

func TestUnknownResourceMethodIsRejected(t *testing.T) {
	authority := &recordingAuthority{decision: allowedDecision()}
	proxy := newTestProxy(t, authority, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream called")
	}), nil)
	response := request(t, proxy.Client(), http.MethodOptions, proxy.URL+"/api/v1/namespaces/demo/pods", "caller")
	response.Body.Close()
	if response.StatusCode != http.StatusNotImplemented {
		t.Fatalf("status = %d", response.StatusCode)
	}
	if len(authority.requests) != 0 {
		t.Fatal("authority called for an unmapped method")
	}
}

func TestHealthDoesNotRequireAuthorization(t *testing.T) {
	proxy := newTestProxy(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("authority called")
	}), http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream called")
	}), nil)
	response := request(t, proxy.Client(), http.MethodGet, proxy.URL+"/healthz", "")
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", response.StatusCode)
	}
}

func TestPlainHTTPAuthorityRequiresExplicitDevelopmentOptIn(t *testing.T) {
	upstream, _ := url.Parse("https://kubernetes.test")
	authority, _ := url.Parse("http://console.test/api/internal/kubernetes/authorize")
	if _, err := NewHandler(Config{Upstream: upstream, AuthorizationURL: authority}); err == nil {
		t.Fatal("plain HTTP authority was accepted without explicit opt-in")
	}
}
