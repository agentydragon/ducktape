package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"io"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestWorkloadListenerUsesLiveSVIDAndReplacesCallerIdentity(t *testing.T) {
	workload := workloadauthtesting.NewWorkload(t, api, testNamespace, "llm-svid")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	reviews := &countingTokenReviews{delegate: api.TokenReviews()}
	serverURL, client := newWorkloadIngressTestServer(t, workload, backendServer.URL, reviews, &resolver.Identities{PolicyNamespace: testPolicyNamespace})

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
	if reviews.calls.Load() != 0 {
		t.Fatalf("TokenReview calls = %d, want none for the SVID listener", reviews.calls.Load())
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
	if metadata["agentplane.namespace"] != workload.Principal.Account.Namespace ||
		metadata["agentplane.service_account"] != workload.Principal.Account.Name ||
		metadata["agentplane.pod_name"] != workload.Principal.PodName ||
		metadata["agentplane.pod_uid"] != workload.Principal.PodUID {
		t.Fatalf("forwarded identity metadata = %#v, want live Sandbox principal %#v", metadata, workload.Principal)
	}
	if forwarded.Get("x-agentplane-subject") != "" {
		t.Fatalf("forwarded caller identity header = %q, want stripped", forwarded.Get("x-agentplane-subject"))
	}
}

func TestWorkloadListenerAttributesAnIdentityCallerByItsIdentity(t *testing.T) {
	workload := workloadauthtesting.NewIdentityWorkload(t, api, testNamespace, "llm-identity", testPolicyNamespace)
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	serverURL, client := newWorkloadIngressTestServer(
		t, workload.Workload, backendServer.URL, api.TokenReviews(), &resolver.Identities{PolicyNamespace: testPolicyNamespace},
	)

	response, err := client.Post(serverURL+"/v1/responses", "application/json", strings.NewReader(`{"model":"test"}`))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("Identity SVID request status = %d, body = %s", response.StatusCode, body)
	}
	requests := backend.snapshotRequests()
	if len(requests) != 1 {
		t.Fatalf("LiteLLM requests = %d, want 1", len(requests))
	}
	var metadata map[string]string
	if err := json.Unmarshal([]byte(requests[0].headers.Get("x-litellm-spend-logs-metadata")), &metadata); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"agentplane.identity":     workload.Subject.Identity.Name,
		"agentplane.identity_uid": workload.Subject.Identity.UID,
		"agentplane.pod_name":     workload.Subject.PodName,
		"agentplane.pod_uid":      workload.Subject.PodUID,
	}
	if !maps.Equal(metadata, want) {
		t.Fatalf("forwarded metadata = %v, want the Identity subject %v alone", metadata, want)
	}
}

func TestWorkloadListenerDoesNotFallBackToValidBearerForStaleSVID(t *testing.T) {
	workload := workloadauthtesting.NewWorkload(t, api, testNamespace, "llm-stale-svid")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	reviews := &countingTokenReviews{delegate: api.TokenReviews()}
	serverURL, client := newWorkloadIngressTestServer(t, workload, backendServer.URL, reviews, &resolver.Identities{PolicyNamespace: testPolicyNamespace})

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
	if reviews.calls.Load() != 0 {
		t.Fatalf("TokenReview calls = %d, want none for the SVID listener", reviews.calls.Load())
	}
	if got := len(backend.snapshotRequests()); got != 0 {
		t.Fatalf("LiteLLM requests = %d, want none for a stale SVID", got)
	}
}

type countingTokenReviews struct {
	delegate auth.TokenReviews
	calls    atomic.Int32
}

func (r *countingTokenReviews) Create(
	ctx context.Context,
	request *authenticationv1.TokenReview,
	options metav1.CreateOptions,
) (*authenticationv1.TokenReview, error) {
	r.calls.Add(1)
	return r.delegate.Create(ctx, request, options)
}

func newWorkloadIngressTestServer(
	t *testing.T,
	workload workloadauthtesting.Workload,
	backendURL string,
	reviews auth.TokenReviews,
	identities *resolver.Identities,
) (string, *http.Client) {
	t.Helper()
	ca := transporttesting.NewCA(t)
	serverCredentials := ca.Issue(t, environment_runtime_transport.LLMIngressURI)
	clientCredentials := ca.IssueWithUsage(t, workload.URI, x509.ExtKeyUsageClientAuth)

	svidResolver, err := auth.NewSVIDResolver(workload.Objects, []string{workload.Sandbox.Namespace}, identities)
	if err != nil {
		t.Fatal(err)
	}
	tokenResolver, err := auth.NewResolver(reviews, testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	rpc := listener.Config{ListenAddress: ":443", TLS: &listener.TLS{CertFile: serverCredentials.CertFile, KeyFile: serverCredentials.KeyFile}, Auth: []listener.Authentication{
		&listener.SPIFFEAuth{CAFile: serverCredentials.CAFile},
		&listener.KubernetesBearerAuth{Audiences: []string{testAudience}},
	}}
	tokenResolver, err = tokenResolver.WithListenerAuthentication(rpc, svidResolver)
	if err != nil {
		t.Fatal(err)
	}
	handler, err := newIngressHandler(config{
		LiteLLMURL:  backendURL,
		LiteLLMKey:  testBackendKey,
		Attribution: defaultAttribution(),
	}, tokenResolver, nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(handler.transport.CloseIdleConnections)

	serverTLS, err := rpc.TLSConfig(environment_runtime_transport.LLMIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	connState, err := environment_runtime_transport.ConfigureHTTPServerTLSExpiry(serverTLS)
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

	clientTLS, err := clientCredentials.ClientTLSConfig(environment_runtime_transport.LLMIngressURI)
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

var _ auth.TokenReviews = (*countingTokenReviews)(nil)

const testPolicyNamespace = "llm-ingress-test-policy"
