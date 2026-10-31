package bridge

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	transformv1 "github.com/agentydragon/ducktape/agentplane/egress/x/iron_proxy/bridge/proto"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/bazelbuild/rules_go/go/runfiles"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/structpb"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	testAudience = "iron-bridge-test"
	testToken    = "hop-token-plain-test-value"
	testTarget   = "api.example.test:443"
)

type tokenReviews struct {
	mu    sync.Mutex
	valid map[string]bool
	count int
}

func (r *tokenReviews) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.count++
	valid := r.valid[review.Spec.Token]
	result := &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: valid,
		Audiences:     []string{testAudience},
	}}
	if valid {
		result.Status.User = authenticationv1.UserInfo{
			Username: "system:serviceaccount:workloads:runner",
			UID:      "service-account-uid",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"runner-0"},
				"authentication.kubernetes.io/pod-uid":  {"pod-uid-0"},
			},
		}
	}
	return result, nil
}

func (r *tokenReviews) revoke(token string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.valid, token)
}

func (r *tokenReviews) reviewCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.count
}

type testBridge struct {
	client  transformv1.TransformServiceClient
	address string
	reviews *tokenReviews
	index   *policy.Index
	ready   *atomic.Bool
}

func newTestBridge(t *testing.T) testBridge {
	t.Helper()
	reviews := &tokenReviews{valid: map[string]bool{testToken: true}}
	// Revocation is controlled at the TokenReview boundary; review each call rather than
	// reusing the production resolver's bounded acceptance cache.
	resolver, err := auth.NewResolver(reviews, testAudience, []string{"workloads"}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}

	index := policy.NewIndex()
	index.Health.MarkSynced()
	index.PutCredential(resources.Credential{
		Name:   "api",
		Source: resources.SecretKey{Name: "keys", Key: "api"},
		Targets: []resources.Target{
			resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"},
			resources.JSONField{Field: "api_key"},
		},
	})
	index.PutSecret(resources.NewSecret("keys", map[string]string{"api": "real-api-secret"}))
	index.Rules.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "egress", Namespace: "policies"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
			Type:          resources.EgressRuleType,
			Hosts:         []string{"api.example.test", "127.0.0.1"},
			Methods:       []string{http.MethodGet},
			Paths:         []string{"/allowed"},
			CredentialRef: &policyapi.CredentialRef{Name: "api"},
		}}},
	})
	index.Rules.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "runner", Namespace: "policies"},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.ServiceAccountRef{Namespace: "workloads", Name: "runner"},
			PolicySets: []string{"egress"},
		},
	})

	ready := &atomic.Bool{}
	ready.Store(true)
	service, err := New(Options{Identity: resolver, Policy: index, Ready: ready.Load})
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	transformv1.RegisterTransformServiceServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})

	dialContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	connection, err := grpc.DialContext(
		dialContext,
		listener.Addr().String(),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithBlock(),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return testBridge{
		client:  transformv1.NewTransformServiceClient(connection),
		address: listener.Addr().String(),
		reviews: reviews,
		index:   index,
		ready:   ready,
	}
}

func TestIronCLIUsesCONNECTIdentityForInnerRequestsAndHonorsRevocation(t *testing.T) {
	lab := newTestBridge(t)
	var upstreamRequests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamRequests.Add(1)
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		_, _ = fmt.Fprintf(w, "%s\n%s\n%s\n%s", r.Header.Get("Authorization"), r.Header.Get(proxyAuthorizationHeader), r.Header.Get(identity.ProjectedTokenHeader), body)
	}))
	t.Cleanup(upstream.Close)
	_, port, err := net.SplitHostPort(strings.TrimPrefix(upstream.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	target := net.JoinHostPort("127.0.0.1", port)

	proxyAddress := startIronProxy(t, lab.address)
	conn, err := net.DialTimeout("tcp", proxyAddress, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	reader := bufio.NewReader(conn)
	if _, err := fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: Bearer %s\r\n\r\n", target, target, testToken); err != nil {
		t.Fatal(err)
	}
	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	connectResponse, err := http.ReadResponse(reader, &http.Request{Method: http.MethodConnect})
	if err != nil {
		t.Fatal(err)
	}
	if connectResponse.StatusCode != http.StatusOK {
		t.Fatalf("Iron CONNECT status = %d, want 200", connectResponse.StatusCode)
	}
	body := "{\n  \"api_key\": \"agentplane-credential-api\",\n  \"note\": \" keep  spacing \"\n}"
	firstStatus, firstBody := exchangeInnerGET(t, conn, reader, target, body)
	wantBody := strings.Replace(body, "agentplane-credential-api", "real-api-secret", 1)
	if firstStatus != http.StatusOK || firstBody != "Bearer real-api-secret\n\n\n"+wantBody {
		t.Fatalf("first tunneled response = %d %q, want 200 with exact rewritten header/body", firstStatus, firstBody)
	}
	if got := upstreamRequests.Load(); got != 1 {
		t.Fatalf("upstream requests after allowed GET = %d, want 1", got)
	}

	lab.reviews.revoke(testToken)
	secondStatus, secondBody := exchangeInnerGET(t, conn, reader, target, body)
	if secondStatus != http.StatusForbidden || !strings.Contains(secondBody, "Forbidden") {
		t.Fatalf("revoked-token tunneled response = %d %q, want 403", secondStatus, secondBody)
	}
	if got := upstreamRequests.Load(); got != 1 {
		t.Errorf("upstream requests after revoked GET = %d, want still 1", got)
	}
	if got := lab.reviews.reviewCount(); got != 3 {
		t.Errorf("TokenReview calls = %d, want CONNECT, allowed GET, and revoked GET", got)
	}
}

func exchangeInnerGET(t *testing.T, conn net.Conn, reader *bufio.Reader, target, body string) (int, string) {
	t.Helper()
	if err := conn.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}
	_, err := fmt.Fprintf(conn, "GET /allowed?preserve=1 HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer agentplane-credential-api\r\nContent-Type: application/json\r\nX-Agentplane-Workload-Token: projected-sensitive\r\nContent-Length: %d\r\n\r\n%s", target, len(body), body)
	if err != nil {
		t.Fatal(err)
	}
	response, err := http.ReadResponse(reader, &http.Request{Method: http.MethodGet})
	if err != nil {
		t.Fatal(err)
	}
	responseBody, err := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, string(responseBody)
}

type ironProxyConfig struct {
	binary          string
	caCert          string
	caKey           string
	upstreamCA      string
	sendRequestBody bool
}

func startIronProxy(t *testing.T, transformAddress string) string {
	t.Helper()
	return startConfiguredIronProxy(t, transformAddress, ironProxyConfig{binary: "binary", sendRequestBody: true})
}

func startConfiguredIronProxy(t *testing.T, transformAddress string, settings ironProxyConfig) string {
	t.Helper()
	files, err := runfiles.New()
	if err != nil {
		t.Fatal(err)
	}
	binary, err := files.Rlocation("ducktape_iron_proxy/" + settings.binary + "_/" + settings.binary)
	if err != nil {
		t.Fatal(err)
	}
	tlsConfig := "  mode: sni-only\n"
	if settings.caCert != "" {
		tlsConfig = fmt.Sprintf("  mode: mitm\n  ca_cert: %q\n  ca_key: %q\n", settings.caCert, settings.caKey)
	}
	// Empty upstream_deny_cidrs is confined to this loopback-only test fixture.
	config := fmt.Sprintf(`dns:
  enabled: false
proxy:
  http_listen: "127.0.0.1:0"
  https_listen: "127.0.0.1:0"
  tunnel_listen: "127.0.0.1:0"
  upstream_deny_cidrs: []
tls:
%stransforms:
  - name: grpc
    config:
      name: %q
      target: %q
      send_request_body: %t
metrics:
  listen: "127.0.0.1:0"
log:
  level: info
`, tlsConfig, TransformName, transformAddress, settings.sendRequestBody)
	configPath := filepath.Join(t.TempDir(), "iron-proxy.yaml")
	if err := os.WriteFile(configPath, []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	command := exec.Command(binary, "--config", configPath)
	command.Env = []string{}
	if settings.upstreamCA != "" {
		command.Env = append(command.Env, "SSL_CERT_FILE="+settings.upstreamCA, "SSL_CERT_DIR="+t.TempDir())
	}
	stderr, err := command.StderrPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = command.Process.Kill()
		_ = command.Wait()
	})
	logs := make(chan map[string]any, 32)
	go func() {
		defer close(logs)
		scanner := bufio.NewScanner(stderr)
		for scanner.Scan() {
			var entry map[string]any
			if json.Unmarshal(scanner.Bytes(), &entry) == nil {
				logs <- entry
			}
		}
	}()
	var seen []string
	timer := time.NewTimer(10 * time.Second)
	defer timer.Stop()
	for {
		select {
		case entry, ok := <-logs:
			if !ok {
				t.Fatalf("Iron CLI exited before tunnel listener started; logs: %s", strings.Join(seen, "\n"))
			}
			seen = append(seen, fmt.Sprint(entry))
			if entry["msg"] == "tunnel proxy starting" {
				address, _ := entry["addr"].(string)
				if address != "" {
					return address
				}
			}
		case <-timer.C:
			t.Fatalf("Iron CLI tunnel listener did not start; logs: %s", strings.Join(seen, "\n"))
		}
	}
}

func TestTransformConnectThenSubstitutesExactGETHeaderAndJSONBody(t *testing.T) {
	lab := newTestBridge(t)
	connect, err := lab.client.TransformRequest(context.Background(), &transformv1.TransformRequestRequest{
		Context: &transformv1.TransformContext{Sni: "api.example.test"},
		Request: &transformv1.HttpRequest{
			Method: http.MethodConnect,
			Url:    "//" + testTarget,
			Host:   testTarget,
			Headers: map[string]*transformv1.HeaderValues{
				proxyAuthorizationHeader: {Values: []string{"Bearer " + testToken}},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if connect.GetAction() != transformv1.TransformAction_TRANSFORM_ACTION_CONTINUE {
		t.Fatalf("CONNECT action = %v, want continue; response=%+v", connect.GetAction(), connect.GetResponse())
	}
	if values := connect.GetModifiedRequest().GetHeaders()[proxyAuthorizationHeader].GetValues(); len(values) > 0 {
		t.Fatalf("CONNECT Proxy-Authorization survived the modified request: %v", values)
	}
	handle := connect.GetAnnotations()[tunnelHandleKey]
	if len(handle) != 64 || strings.Contains(handle, testToken) {
		t.Fatalf("CONNECT annotation does not contain only an opaque handle: %q", handle)
	}

	annotations, err := structpb.NewStruct(map[string]any{tunnelHandleKey: handle})
	if err != nil {
		t.Fatal(err)
	}
	body := []byte("{\n  \"api_key\": \"agentplane-credential-api\",\n  \"note\": \" keep  spacing \"\n}")
	get, err := lab.client.TransformRequest(context.Background(), &transformv1.TransformRequestRequest{
		Context: &transformv1.TransformContext{
			Sni: "api.example.test",
			Tunnel: &transformv1.TunnelInfo{
				Target: testTarget,
				RequestTransforms: []*transformv1.TunnelRequestTransform{{
					Name:        TransformName,
					Annotations: annotations,
				}},
			},
		},
		Request: &transformv1.HttpRequest{
			Method: http.MethodGet,
			Url:    "/allowed?preserve=1",
			Host:   "api.example.test",
			Headers: map[string]*transformv1.HeaderValues{
				"Authorization":               {Values: []string{"Bearer agentplane-credential-api"}},
				"Content-Type":                {Values: []string{"application/json"}},
				identity.ProjectedTokenHeader: {Values: []string{"sensitive-projected-token"}},
			},
			Body: body,
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if get.GetAction() != transformv1.TransformAction_TRANSFORM_ACTION_CONTINUE {
		t.Fatalf("GET action = %v, want continue; response=%+v", get.GetAction(), get.GetResponse())
	}
	modified := get.GetModifiedRequest()
	if modified == nil {
		t.Fatal("GET did not return its policy substitutions")
	}
	if got := modified.GetHeaders()["Authorization"].GetValues(); len(got) != 1 || got[0] != "Bearer real-api-secret" {
		t.Errorf("rewritten Authorization = %v, want [Bearer real-api-secret]", got)
	}
	if values := modified.GetHeaders()[identity.ProjectedTokenHeader].GetValues(); len(values) > 0 {
		t.Errorf("unsupported projected token header was forwarded: %v", values)
	}
	if values := modified.GetHeaders()[proxyAuthorizationHeader].GetValues(); len(values) > 0 {
		t.Errorf("Proxy-Authorization was forwarded: %v", values)
	}
	wantBody := strings.Replace(string(body), "agentplane-credential-api", "real-api-secret", 1)
	if got := string(modified.GetBody()); got != wantBody {
		t.Errorf("rewritten body = %q, want exact-byte substitution %q", got, wantBody)
	}
	if got := modified.GetHeaders()["Content-Type"].GetValues(); len(got) != 1 || got[0] != "application/json" {
		t.Errorf("unchanged Content-Type = %v, want [application/json]", got)
	}
	if got := lab.reviews.reviewCount(); got != 2 {
		t.Errorf("TokenReview calls = %d, want one CONNECT review and one inherited GET re-review", got)
	}
}

func TestTransformRequestRejectsRevokedInheritedToken(t *testing.T) {
	lab := newTestBridge(t)
	handle := establishTunnel(t, lab)
	lab.reviews.revoke(testToken)

	response, err := lab.client.TransformRequest(context.Background(), innerRequest(t, handle, &transformv1.HttpRequest{
		Method: http.MethodGet,
		Url:    "/allowed",
		Host:   "api.example.test",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if response.GetAction() != transformv1.TransformAction_TRANSFORM_ACTION_REJECT {
		t.Fatalf("revoked-token action = %v, want reject", response.GetAction())
	}
	if got := response.GetResponse().GetStatusCode(); got != http.StatusForbidden {
		t.Errorf("revoked-token status = %d, want 403", got)
	}
	if got := response.GetResponse().GetHeaders()[egressReasonHeader].GetValues(); len(got) != 1 || got[0] != "denied; reason="+string(decision.TokenRejected) {
		t.Errorf("revoked-token reason header = %v", got)
	}
	if got := lab.reviews.reviewCount(); got != 2 {
		t.Errorf("TokenReview calls = %d, want CONNECT review plus revocation check", got)
	}
}

func TestTransformRequestReadinessRejectsBeforeTokenReview(t *testing.T) {
	lab := newTestBridge(t)
	lab.ready.Store(false)
	response, err := lab.client.TransformRequest(context.Background(), &transformv1.TransformRequestRequest{
		Request: &transformv1.HttpRequest{
			Method: http.MethodConnect,
			Url:    "//" + testTarget,
			Host:   testTarget,
			Headers: map[string]*transformv1.HeaderValues{
				proxyAuthorizationHeader: {Values: []string{"Bearer " + testToken}},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetAction() != transformv1.TransformAction_TRANSFORM_ACTION_REJECT || response.GetResponse().GetStatusCode() != http.StatusServiceUnavailable {
		t.Fatalf("unready action/status = %v/%d, want reject/503", response.GetAction(), response.GetResponse().GetStatusCode())
	}
	if got := lab.reviews.reviewCount(); got != 0 {
		t.Errorf("TokenReview calls before readiness = %d, want 0", got)
	}
}

func TestTransformRequestRequiresAvailablePolicyIndex(t *testing.T) {
	lab := newTestBridge(t)
	lab.index.Health.MarkDraining()
	response, err := lab.client.TransformRequest(context.Background(), &transformv1.TransformRequestRequest{
		Request: &transformv1.HttpRequest{
			Method: http.MethodConnect,
			Url:    "//" + testTarget,
			Host:   testTarget,
			Headers: map[string]*transformv1.HeaderValues{
				proxyAuthorizationHeader: {Values: []string{"Bearer " + testToken}},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetAction() != transformv1.TransformAction_TRANSFORM_ACTION_REJECT || response.GetResponse().GetStatusCode() != http.StatusServiceUnavailable {
		t.Fatalf("draining-index action/status = %v/%d, want reject/503", response.GetAction(), response.GetResponse().GetStatusCode())
	}
	if got := lab.reviews.reviewCount(); got != 0 {
		t.Errorf("TokenReview calls with unavailable index = %d, want 0", got)
	}
}

func establishTunnel(t *testing.T, lab testBridge) string {
	t.Helper()
	response, err := lab.client.TransformRequest(context.Background(), &transformv1.TransformRequestRequest{
		Request: &transformv1.HttpRequest{
			Method: http.MethodConnect,
			Url:    "//" + testTarget,
			Host:   testTarget,
			Headers: map[string]*transformv1.HeaderValues{
				proxyAuthorizationHeader: {Values: []string{"Bearer " + testToken}},
			},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetAction() != transformv1.TransformAction_TRANSFORM_ACTION_CONTINUE {
		t.Fatalf("CONNECT action = %v, want continue: %s", response.GetAction(), fmt.Sprint(response.GetResponse()))
	}
	return response.GetAnnotations()[tunnelHandleKey]
}

func innerRequest(t *testing.T, handle string, request *transformv1.HttpRequest) *transformv1.TransformRequestRequest {
	t.Helper()
	annotations, err := structpb.NewStruct(map[string]any{tunnelHandleKey: handle})
	if err != nil {
		t.Fatal(err)
	}
	return &transformv1.TransformRequestRequest{
		Context: &transformv1.TransformContext{
			Tunnel: &transformv1.TunnelInfo{
				Target: testTarget,
				RequestTransforms: []*transformv1.TunnelRequestTransform{{
					Name:        TransformName,
					Annotations: annotations,
				}},
			},
		},
		Request: request,
	}
}
