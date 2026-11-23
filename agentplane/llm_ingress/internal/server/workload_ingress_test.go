package server

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
)

func TestWorkloadListenerUsesLiveSVIDAndReplacesCallerIdentity(t *testing.T) {
	workload := workloadauthtesting.NewIdentityWorkload(t, api, testNamespace, "llm-svid", testPolicyNamespace).Workload
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	serverURL, client := newWorkloadIngressTestServer(t, workload, backendServer.URL, &resolver.Identities{PolicyNamespace: testPolicyNamespace})

	request, err := http.NewRequest(http.MethodPost, serverURL+"/v1/responses", strings.NewReader(`{"model":"test"}`))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("x-agentplane-subject", "attacker/forged")
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("workload SVID request status = %d, body = %s", response.StatusCode, body)
	}
	requests := backend.snapshotRequests()
	if len(requests) != 1 {
		t.Fatalf("LiteLLM requests = %d, want 1", len(requests))
	}
	forwarded := requests[0].headers
	if got := forwarded.Get("Authorization"); got != "Bearer "+testBackendKey {
		t.Fatalf("forwarded Authorization = %q, want server-held key", got)
	}
	var metadata map[string]string
	if err := json.Unmarshal([]byte(forwarded.Get("x-litellm-spend-logs-metadata")), &metadata); err != nil {
		t.Fatal(err)
	}
	if metadata["agentplane.identity"] != workload.Identity.Name ||
		metadata["agentplane.identity_uid"] != string(workload.Identity.UID) ||
		metadata["agentplane.pod_name"] != workload.Subject.PodName ||
		metadata["agentplane.pod_uid"] != workload.Subject.PodUID {
		t.Fatalf("forwarded Identity metadata = %#v, want live Sandbox Identity %#v", metadata, workload.Subject)
	}
	if forwarded.Get("x-agentplane-subject") != "" {
		t.Fatalf("forwarded caller identity header = %q, want stripped", forwarded.Get("x-agentplane-subject"))
	}
	forgedSession, err := http.NewRequest(http.MethodPost, serverURL+"/v1/responses", strings.NewReader(`{"model":"test"}`))
	if err != nil {
		t.Fatal(err)
	}
	forgedSession.Header.Set("x-agentplane-session-id", "00000000-0000-4000-8000-000000000077")
	refused, err := client.Do(forgedSession)
	if err != nil {
		t.Fatal(err)
	}
	defer refused.Body.Close()
	if refused.StatusCode != http.StatusForbidden || len(backend.snapshotRequests()) != 1 {
		t.Fatalf("workload Session header status=%d, want forbidden before backend", refused.StatusCode)
	}
}

func TestWorkloadListenerDoesNotFallBackToValidBearerForStaleSVID(t *testing.T) {
	workload := workloadauthtesting.NewIdentityWorkload(t, api, testNamespace, "llm-stale-svid", testPolicyNamespace).Workload
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	serverURL, client := newWorkloadIngressTestServer(t, workload, backendServer.URL, &resolver.Identities{PolicyNamespace: testPolicyNamespace})

	workload.Issue(t, "ffffffffffffffffffffffffffffffff")
	validBearer := workloadToken(t, "llm-bearer-fallback")
	request, err := http.NewRequest(http.MethodPost, serverURL+"/v1/responses", strings.NewReader(`{"model":"test"}`))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+validBearer)
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("stale SVID plus valid bearer status = %d, body = %s, want 401", response.StatusCode, body)
	}
	if got := len(backend.snapshotRequests()); got != 0 {
		t.Fatalf("LiteLLM requests = %d, want none for a stale SVID", got)
	}
}

func newWorkloadIngressTestServer(
	t *testing.T,
	workload workloadauthtesting.Workload,
	backendURL string,
	identities *resolver.Identities,
) (string, *http.Client) {
	t.Helper()
	ca := transporttesting.NewCA(t)
	serverCredentials := ca.Issue(t, peertls.LLMIngressURI)
	clientCredentials := ca.IssueWithUsage(t, workload.URI, x509.ExtKeyUsageClientAuth)

	svidResolver, err := auth.NewSVIDResolver(workload.Objects, []string{workload.Sandbox.Namespace}, identities)
	if err != nil {
		t.Fatal(err)
	}
	handler, err := newIngressHandler(config{
		LiteLLMURL:  backendURL,
		LiteLLMKey:  testBackendKey,
		Attribution: defaultAttribution(),
	}, svidResolver, nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(handler.transport.CloseIdleConnections)

	serverTLS, err := serverCredentials.WorkloadServerTLSConfig(peertls.LLMIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	connState, err := peertls.ConfigureHTTPServerTLSExpiry(serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.NextProtos = []string{"http/1.1"}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{
		Handler:   handler,
		TLSConfig: serverTLS,
		ConnState: connState,
	}
	tlsListener := tls.NewListener(listener, serverTLS)
	go func() {
		if err := server.Serve(tlsListener); err != nil && err != http.ErrServerClosed {
			t.Errorf("serve test workload listener: %v", err)
		}
	}()
	t.Cleanup(func() { _ = server.Close() })

	clientTLS, err := clientCredentials.ClientTLSConfig(peertls.LLMIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.TLSClientConfig = clientTLS
	client := &http.Client{Transport: transport}
	t.Cleanup(client.CloseIdleConnections)
	return "https://" + listener.Addr().String(), client
}

const testPolicyNamespace = "llm-ingress-test-policy"
