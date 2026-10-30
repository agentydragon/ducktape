package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel/baggage"

	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/bazelbuild/rules_go/go/runfiles"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/client-go/rest"
)

const (
	testAudience   = "test-llm-ingress"
	testNamespace  = "test-sandboxes"
	testBackendKey = "sk-server-held-test-key"
)

var api *kubeapiserver.Server

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error {
		api = server
		return nil
	}))
}

var toolStream = []byte(
	"event: response.output_item.added\n" +
		"data: {\"type\":\"response.output_item.added\",\"item\":{\"type\":\"function_call\",\"name\":\"shell\",\"call_id\":\"call_1\"}}\n\n" +
		"event: response.function_call_arguments.delta\n" +
		"data: {\"type\":\"response.function_call_arguments.delta\",\"delta\":\"{\\\"command\\\":\\\"pwd\\\"}\"}\n\n" +
		"event: response.completed\n" +
		"data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}\n\n",
)
var anthropicError = []byte(`{"type":"error","error":{"type":"overloaded_error","message":"backend busy"}}`)

// workload is a new Pod running as the ServiceAccount name in testNamespace and a token for
// audiences bound to it.
func workload(t *testing.T, name string, audiences ...string) (*corev1.Pod, string) {
	t.Helper()
	pod, err := api.Pod(t.Context(), auth.Account{Namespace: testNamespace, Name: name})
	if err != nil {
		t.Fatal(err)
	}
	token, err := api.PodToken(t.Context(), pod, audiences...)
	if err != nil {
		t.Fatal(err)
	}
	return pod, token
}

// workloadToken is a token for testAudience bound to a new Pod running as the ServiceAccount name in testNamespace.
func workloadToken(t *testing.T, name string) string {
	t.Helper()
	_, token := workload(t, name, testAudience)
	return token
}

func newIngressTestServer(t *testing.T, reviews auth.TokenReviews, backendURL string, models ...modelConfig) *httptest.Server {
	t.Helper()
	return newConfiguredIngressTestServer(t, reviews, config{
		LiteLLMURL: backendURL,
		LiteLLMKey: testBackendKey,
		Models:     models,
	})
}

func newConfiguredIngressTestServer(t *testing.T, reviews auth.TokenReviews, c config, options ...auth.Option) *httptest.Server {
	t.Helper()
	if len(c.Attribution.JSONHeaders) == 0 && len(c.Attribution.Baggage) == 0 {
		c.Attribution = defaultAttribution()
	}
	resolver, err := auth.NewResolver(reviews, testAudience, []string{testNamespace}, options...)
	if err != nil {
		t.Fatal(err)
	}
	handler, err := newIngressHandler(c, resolver, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return server
}

type capturedRequest struct {
	method  string
	path    string
	headers http.Header
	body    []byte
}

type fakeLiteLLM struct {
	mu              sync.Mutex
	requests        []capturedRequest
	streamStarted   chan struct{}
	continueStream  chan struct{}
	streamCancelled chan struct{}
}

func newFakeLiteLLM() *fakeLiteLLM {
	return &fakeLiteLLM{
		streamStarted:   make(chan struct{}, 1),
		continueStream:  make(chan struct{}),
		streamCancelled: make(chan struct{}, 1),
	}
}

func newBackendServer(t *testing.T, fake *fakeLiteLLM) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(fake.handle))
	t.Cleanup(server.Close)
	return server
}

func (f *fakeLiteLLM) handle(writer http.ResponseWriter, request *http.Request) {
	body, err := io.ReadAll(request.Body)
	if err != nil {
		http.Error(writer, "could not read request", http.StatusBadRequest)
		return
	}
	f.mu.Lock()
	f.requests = append(f.requests, capturedRequest{
		method: request.Method, path: request.URL.RequestURI(), headers: request.Header.Clone(), body: body,
	})
	f.mu.Unlock()

	switch request.URL.Path {
	case "/compressed":
		var compressed bytes.Buffer
		zipper := gzip.NewWriter(&compressed)
		if _, err := zipper.Write(toolStream); err != nil {
			http.Error(writer, "compress failed", http.StatusInternalServerError)
			return
		}
		if err := zipper.Close(); err != nil {
			http.Error(writer, "compress failed", http.StatusInternalServerError)
			return
		}
		writer.Header().Set("Content-Type", "text/event-stream")
		writer.Header().Set("Content-Encoding", "gzip")
		writer.Header().Add("Set-Cookie", "first=1")
		writer.Header().Add("Set-Cookie", "second=2")
		writer.Header().Set("Connection", "x-drop")
		writer.Header().Set("x-drop", "hop-only")
		writer.Header().Set("Authorization", "Bearer "+testBackendKey)
		_, _ = writer.Write(compressed.Bytes())
	case "/gated-stream":
		writer.Header().Set("Content-Type", "text/event-stream")
		writer.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(writer, "first\n")
		writer.(http.Flusher).Flush()
		f.streamStarted <- struct{}{}
		select {
		case <-request.Context().Done():
			f.streamCancelled <- struct{}{}
			return
		case <-f.continueStream:
			_, _ = io.WriteString(writer, "second\n")
			writer.(http.Flusher).Flush()
		}
	case "/truncated":
		writer.Header().Set("Content-Length", "100")
		writer.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(writer, "partial")
		writer.(http.Flusher).Flush()
	case "/v1/messages":
		writer.Header().Set("Content-Type", "application/json")
		writer.Header().Set("x-backend-shape", "messages")
		writer.WriteHeader(529)
		_, _ = writer.Write(anthropicError)
	default:
		writer.Header().Set("Content-Type", "text/event-stream")
		writer.Header().Set("x-backend-shape", "responses")
		writer.WriteHeader(http.StatusOK)
		for _, chunk := range [][]byte{toolStream[:97], toolStream[97:241], toolStream[241:]} {
			_, _ = writer.Write(chunk)
			writer.(http.Flusher).Flush()
		}
	}
}

func (f *fakeLiteLLM) snapshotRequests() []capturedRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]capturedRequest(nil), f.requests...)
}

func testClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &http.Client{Transport: transport, Timeout: 5 * time.Second}
}

func request(t *testing.T, client *http.Client, method, url, body string, headers http.Header) (*http.Response, []byte) {
	t.Helper()
	req, err := http.NewRequest(method, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header = headers.Clone()
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	result, err := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	return response, result
}

func bearer(token string) http.Header {
	return http.Header{"Authorization": {"Bearer " + token}}
}

func TestTwoWorkloadsShareBackendKeyAndKeepVerifiedMetadataDistinct(t *testing.T) {
	podA, tokenA := workload(t, "test-sandbox-a", testAudience)
	podB, tokenB := workload(t, "test-sandbox-b", testAudience)
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	body := `{"model":"chatgpt-sub/oai-responses/gpt-5.6-luna","stream":true,"metadata":{"sandbox_name":"forged"}}`
	firstHeaders := bearer(tokenA)
	firstHeaders.Set("Content-Type", "application/json")
	firstHeaders.Set("x-litellm-spend-logs-metadata", `{"agentplane.sandbox_name":"forged"}`)
	firstHeaders.Set("x-litellm-customer-id", "forged")
	firstHeaders.Set("x-api-key", "forged")
	firstHeaders.Set("x-litellm-api-key", "forged")
	for _, name := range []string{"x-agentplane-pod-uid", "x-sandbox-name", "x-pod-uid", "x-agent-id", "x-thread-id"} {
		firstHeaders.Set(name, "forged")
	}
	first, firstBody := request(t, client, http.MethodPost, ingress.URL+"/v1/responses?trace=kept", body, firstHeaders)
	second, secondBody := request(t, client, http.MethodPost, ingress.URL+"/v1/responses?trace=kept", body, bearer(tokenB))
	if first.StatusCode != http.StatusOK || second.StatusCode != http.StatusOK {
		t.Fatalf("responses: first=%d %s, second=%d %s", first.StatusCode, firstBody, second.StatusCode, secondBody)
	}
	if !bytes.Equal(firstBody, toolStream) || !bytes.Equal(secondBody, toolStream) {
		t.Fatal("provider-native stream bytes changed")
	}
	if first.Header.Get("Content-Type") != "text/event-stream" || first.Header.Get("x-backend-shape") != "responses" {
		t.Fatalf("response headers were not passed through: %v", first.Header)
	}

	requests := backend.snapshotRequests()
	if len(requests) != 2 {
		t.Fatalf("backend saw %d requests, want 2", len(requests))
	}
	for _, got := range requests {
		if got.method != http.MethodPost || got.path != "/v1/responses?trace=kept" {
			t.Fatalf("backend request target = %s %s", got.method, got.path)
		}
		if !bytes.Equal(got.body, []byte(body)) {
			t.Fatal("provider-native request body changed")
		}
		if got.headers.Get("Authorization") != "Bearer "+testBackendKey {
			t.Fatalf("backend authorization = %q", got.headers.Get("Authorization"))
		}
		for _, name := range []string{
			"x-litellm-customer-id", "x-api-key", "x-litellm-api-key", "x-agentplane-pod-uid",
			"x-sandbox-name", "x-pod-uid", "x-agent-id", "x-thread-id",
		} {
			if got.headers.Get(name) != "" {
				t.Errorf("forged header %q reached backend", name)
			}
		}
	}
	var firstMetadata, secondMetadata map[string]string
	if err := json.Unmarshal([]byte(requests[0].headers.Get("x-litellm-spend-logs-metadata")), &firstMetadata); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(requests[1].headers.Get("x-litellm-spend-logs-metadata")), &secondMetadata); err != nil {
		t.Fatal(err)
	}
	wantFirst := map[string]string{
		"agentplane.namespace":       testNamespace,
		"agentplane.service_account": "test-sandbox-a",
		"agentplane.pod_name":        podA.Name,
		"agentplane.pod_uid":         string(podA.UID),
	}
	if !maps.Equal(firstMetadata, wantFirst) || secondMetadata["agentplane.pod_name"] != podB.Name ||
		secondMetadata["agentplane.pod_uid"] != string(podB.UID) {
		t.Fatalf("verified metadata: first=%v second=%v", firstMetadata, secondMetadata)
	}
	for _, transcript := range [][]byte{firstBody, secondBody} {
		if bytes.Contains(transcript, []byte(tokenA)) || bytes.Contains(transcript, []byte(tokenB)) ||
			bytes.Contains(transcript, []byte(testBackendKey)) {
			t.Fatal("a credential appeared in a caller response")
		}
	}
}

func TestAnthropicStatusErrorBodyAndRequestShapePassUnchanged(t *testing.T) {
	tokenA := workloadToken(t, "test-sandbox-a")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	body := `{"model":"anthropic-api/ant-messages/claude-haiku-4-5-20251001","max_tokens":32,"messages":[]}`
	response, gotBody := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", body, bearer(tokenA))
	if response.StatusCode != 529 || !bytes.Equal(gotBody, anthropicError) ||
		response.Header.Get("Content-Type") != "application/json" || response.Header.Get("x-backend-shape") != "messages" {
		t.Fatalf("response: status=%d headers=%v body=%s", response.StatusCode, response.Header, gotBody)
	}
	got := backend.snapshotRequests()
	if len(got) != 1 || !bytes.Equal(got[0].body, []byte(body)) || got[0].headers.Get("Authorization") != "Bearer "+testBackendKey {
		t.Fatalf("backend request was not passed through: %+v", got)
	}
}

func TestMissingMalformedUnknownAndWrongAudienceBearersFailBeforeBackend(t *testing.T) {
	_, wrongAudience := workload(t, "test-live", "test-elsewhere")
	reviews := &kubeapiserver.CountedReviews{TokenReviews: api.TokenReviews()}
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	attempts := []*http.Response{
		doRequest(t, client, ingress.URL+"/v1/messages", http.Header{}),
		doRequest(t, client, ingress.URL+"/v1/messages", http.Header{"Authorization": {"not-a-bearer"}}),
		doRequest(t, client, ingress.URL+"/v1/messages", bearer("test-unknown-token")),
		doRequest(t, client, ingress.URL+"/v1/messages", bearer(wrongAudience)),
	}
	for _, response := range attempts {
		body, err := io.ReadAll(response.Body)
		_ = response.Body.Close()
		if err != nil {
			t.Fatal(err)
		}
		if response.StatusCode != http.StatusUnauthorized || !strings.Contains(string(body), `"detail":"invalid workload bearer"`) {
			t.Errorf("refusal: status=%d body=%s", response.StatusCode, body)
		}
	}
	if got := backend.snapshotRequests(); len(got) != 0 {
		t.Fatalf("unauthenticated requests reached backend: %d", len(got))
	}
	if got := reviews.Calls.Load(); got != 2 {
		t.Fatalf("TokenReview calls = %d, want only unknown and wrong-audience tokens", got)
	}
}

func doRequest(t *testing.T, client *http.Client, url string, headers http.Header) *http.Response {
	t.Helper()
	req, err := http.NewRequest(http.MethodPost, url, strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	req.Header = headers.Clone()
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func TestAuthenticatedWorkloadWithoutSandboxOwnerIsServedAsItsServiceAccount(t *testing.T) {
	pod, unowned := workload(t, "test-unowned", testAudience)
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/responses",
		`{"model":"chatgpt-sub/oai-responses/gpt-5.6-luna","stream":true}`, bearer(unowned))
	if response.StatusCode != http.StatusOK || !bytes.Equal(body, toolStream) {
		t.Fatalf("status=%d body=%s", response.StatusCode, body)
	}
	got := backend.snapshotRequests()
	var metadata map[string]string
	if len(got) != 1 || json.Unmarshal([]byte(got[0].headers.Get("x-litellm-spend-logs-metadata")), &metadata) != nil ||
		metadata["agentplane.pod_name"] != pod.Name || metadata["agentplane.service_account"] != "test-unowned" {
		t.Fatalf("metadata did not follow verified service account: %v", metadata)
	}
}

func TestModelConfigLookupIsAuthenticatedAndAnsweredFromConfiguration(t *testing.T) {
	const model = "ollama/oai-chat/qwen3.8-flash-next-iq4xs-256k"
	tokenA := workloadToken(t, "test-sandbox-a")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL, modelConfig{
		Model: model, TotalContextBudgetTokens: 256 * 1024,
	})
	client := testClient()
	defer client.CloseIdleConnections()

	found, foundBody := request(t, client, http.MethodGet, ingress.URL+"/agentplane/model-config?model="+model, "", bearer(tokenA))
	missing, missingBody := request(t, client, http.MethodGet, ingress.URL+"/agentplane/model-config?model=unlisted", "", bearer(tokenA))
	unauthenticated := doRequest(t, client, ingress.URL+"/agentplane/model-config?model="+model, http.Header{})
	unauthenticatedBody, _ := io.ReadAll(unauthenticated.Body)
	_ = unauthenticated.Body.Close()
	missingParameter, missingParameterBody := request(t, client, http.MethodGet, ingress.URL+"/agentplane/model-config", "", bearer(tokenA))
	if found.StatusCode != http.StatusOK || string(foundBody) != `{"model":"`+model+`","total_context_budget_tokens":262144}`+"\n" ||
		missing.StatusCode != http.StatusOK || string(missingBody) != "null" ||
		unauthenticated.StatusCode != http.StatusUnauthorized ||
		missingParameter.StatusCode != http.StatusUnprocessableEntity ||
		!strings.Contains(string(missingParameterBody), `"Field required"`) {
		t.Fatalf("found=%d %s missing=%d %s unauthorized=%d %s no-model=%d %s",
			found.StatusCode, foundBody, missing.StatusCode, missingBody,
			unauthenticated.StatusCode, unauthenticatedBody, missingParameter.StatusCode, missingParameterBody)
	}
	if got := backend.snapshotRequests(); len(got) != 0 {
		t.Fatalf("model config lookup reached LiteLLM: %d requests", len(got))
	}
}

func TestStreamIsIncrementalAndClientDisconnectCancelsBackend(t *testing.T) {
	tokenA := workloadToken(t, "test-sandbox-a")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	req, err := http.NewRequest(http.MethodPost, ingress.URL+"/gated-stream", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header = bearer(tokenA)
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status=%d", response.StatusCode)
	}
	first, err := bufio.NewReader(response.Body).ReadString('\n')
	if err != nil || first != "first\n" {
		t.Fatalf("first streamed chunk = %q, err=%v", first, err)
	}
	select {
	case <-backend.streamStarted:
	case <-time.After(5 * time.Second):
		t.Fatal("backend stream did not start")
	}
	_ = response.Body.Close()
	select {
	case <-backend.streamCancelled:
	case <-time.After(5 * time.Second):
		t.Fatal("client disconnect did not cancel backend request")
	}
}

func TestCompressedBytesDuplicateHeadersAndHopHeaderRemoval(t *testing.T) {
	tokenA := workloadToken(t, "test-sandbox-a")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	headers := bearer(tokenA)
	headers.Set("Accept-Encoding", "gzip")
	headers.Set("Connection", "x-drop")
	headers.Set("x-drop", "hop-only")
	response, err := client.Do(mustRequest(t, http.MethodPost, ingress.URL+"/compressed", headers))
	if err != nil {
		t.Fatal(err)
	}
	raw, err := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	zipper, err := gzip.NewReader(bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	decompressed, err := io.ReadAll(zipper)
	_ = zipper.Close()
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || !bytes.Equal(decompressed, toolStream) ||
		response.Header.Get("Content-Encoding") != "gzip" {
		t.Fatalf("status=%d content-encoding=%q decompressed=%q", response.StatusCode, response.Header.Get("Content-Encoding"), decompressed)
	}
	if got := response.Header.Values("Set-Cookie"); len(got) != 2 || got[0] != "first=1" || got[1] != "second=2" {
		t.Fatalf("duplicate Set-Cookie headers = %v", got)
	}
	if response.Header.Get("Authorization") != "" || response.Header.Get("x-drop") != "" {
		t.Fatalf("sensitive or hop header reached caller: %v", response.Header)
	}
	got := backend.snapshotRequests()
	if len(got) != 1 || got[0].headers.Get("x-drop") != "" {
		t.Fatalf("hop header reached backend: %+v", got)
	}
}

func mustRequest(t *testing.T, method, url string, headers http.Header) *http.Request {
	t.Helper()
	req, err := http.NewRequest(method, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header = headers.Clone()
	return req
}

// forwardedHeaders applies what Rewrite applies to the outgoing request headers.
func forwardedHeaders(headers http.Header, principal auth.Principal, attribution attributionConfig) (http.Header, error) {
	verified, err := verifiedRequestHeaders(principal, testBackendKey, attribution, "")
	if err != nil {
		return nil, err
	}
	forwarded := headers.Clone()
	dropUntrustedRequestHeaders(forwarded, attribution)
	maps.Copy(forwarded, verified)
	return forwarded, nil
}

func TestCredentialAndIdentityHeadersAreReplacedByVerifiedOnes(t *testing.T) {
	headers := http.Header{
		"Authorization":                 {"Bearer workload-token"},
		"X-Api-Key":                     {"forged"},
		"X-Litellm-Api-Key":             {"forged"},
		"X-Litellm-Spend-Logs-Metadata": {`{"forged":true}`},
		"X-Agentplane-Pod-Uid":          {"forged"},
		"Baggage":                       {"litellm.metadata.spend_logs_metadata.agentplane.namespace=forged,ordinary=untrusted"},
		"Content-Length":                {"8"},
		"X-Ordinary":                    {"kept"},
	}
	principal := auth.Principal{
		Account: auth.Account{Namespace: testNamespace, Name: "sandbox-a"},
		PodName: "sandbox-a", PodUID: "pod-a-uid",
	}
	attribution := defaultAttribution()
	attribution.Baggage = []attributionField{
		{Key: "litellm.metadata.spend_logs_metadata.agentplane.namespace", Source: verifiedNamespace},
		{Key: "litellm.metadata.spend_logs_metadata.agentplane.service_account", Source: verifiedServiceAccount},
		{Key: "litellm.metadata.spend_logs_metadata.agentplane.pod_name", Source: verifiedPodName},
		{Key: "litellm.metadata.spend_logs_metadata.agentplane.pod_uid", Source: verifiedPodUID},
	}
	forwarded, err := forwardedHeaders(headers, principal, attribution)
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{
		"X-Api-Key", "X-Litellm-Api-Key",
		"X-Agentplane-Pod-Uid", "Content-Length",
	} {
		if forwarded.Get(name) != "" {
			t.Errorf("request header %q was retained: %v", name, forwarded)
		}
	}
	if forwarded.Get("Authorization") != "Bearer "+testBackendKey || forwarded.Get("X-Ordinary") != "kept" {
		t.Fatalf("request headers were not rebuilt safely: %v", forwarded)
	}
	if got := forwarded.Get("Baggage"); strings.Contains(got, "forged") || strings.Contains(got, "ordinary") ||
		!strings.Contains(got, "litellm.metadata.spend_logs_metadata.agentplane.namespace="+testNamespace) {
		t.Fatalf("baggage was not rebuilt from verified identity: %q", got)
	}
	parsedBaggage, err := baggage.Parse(forwarded.Get("Baggage"))
	if err != nil || len(parsedBaggage.Members()) != 4 ||
		parsedBaggage.Member("litellm.metadata.spend_logs_metadata.agentplane.pod_uid").Value() != principal.PodUID {
		t.Fatalf("forwarded baggage cannot be extracted as four verified claims: %v, err=%v", parsedBaggage, err)
	}
	for _, test := range []struct {
		name              string
		attribution       attributionConfig
		wantSpendMetadata bool
		wantBaggage       bool
	}{
		{name: "spend only", attribution: defaultAttribution(), wantSpendMetadata: true},
		{name: "baggage only", attribution: attributionConfig{Baggage: attribution.Baggage}, wantBaggage: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			only, err := forwardedHeaders(headers, principal, test.attribution)
			if err != nil {
				t.Fatal(err)
			}
			if (only.Get("X-Litellm-Spend-Logs-Metadata") != "") != test.wantSpendMetadata ||
				(only.Get("Baggage") != "") != test.wantBaggage {
				t.Fatalf("unexpected attribution headers: %v", only)
			}
		})
	}
	custom := attributionConfig{
		JSONHeaders: []attributionJSONHeader{{Name: "X-Custom-Verified", Fields: []attributionField{{Key: "Pod.Name", Source: verifiedPodName}}}},
		Baggage:     []attributionField{{Key: "custom.PodUid", Source: verifiedPodUID}},
	}
	headers.Set("X-Custom-Verified", `{"Pod.Name":"forged"}`)
	customForwarded, err := forwardedHeaders(headers, principal, custom)
	if err != nil {
		t.Fatal(err)
	}
	if customForwarded.Get("X-Litellm-Spend-Logs-Metadata") != "" || customForwarded.Get("X-Custom-Verified") != `{"Pod.Name":"sandbox-a"}` ||
		customForwarded.Get("Baggage") != "custom.PodUid=pod-a-uid" {
		t.Fatalf("custom attribution did not preserve key spelling and verified sources: %v", customForwarded)
	}
	var metadata map[string]string
	if err := json.Unmarshal([]byte(forwarded.Get("X-Litellm-Spend-Logs-Metadata")), &metadata); err != nil ||
		metadata["agentplane.pod_uid"] != "pod-a-uid" {
		t.Fatalf("trusted metadata was not stamped: %v, err=%v", metadata, err)
	}
}

func TestTruncatedBackendResponseDoesNotLookLikeSuccessfulEndOfStream(t *testing.T) {
	tokenA := workloadToken(t, "test-sandbox-a")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	req, err := http.NewRequest(http.MethodPost, ingress.URL+"/truncated", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header = bearer(tokenA)
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, readErr := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if readErr == nil {
		t.Fatalf("truncated backend body looked complete: status=%d body=%q", response.StatusCode, body)
	}
}

func TestRevokedAndDisallowedNamespaceTokensAreRefused(t *testing.T) {
	podA, tokenA := workload(t, "test-sandbox-a", testAudience)
	tokenB, err := api.WorkloadToken(t.Context(), auth.Account{Namespace: "test-elsewhere", Name: "test-sandbox-b"}, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	reviews := &kubeapiserver.CountedReviews{TokenReviews: api.TokenReviews()}
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	// The resolver reviews every call, so the revocation shows on the next request.
	ingress := newConfiguredIngressTestServer(t, reviews, config{LiteLLMURL: backendServer.URL, LiteLLMKey: testBackendKey},
		auth.WithVerdictTTL(0))
	client := testClient()
	defer client.CloseIdleConnections()

	response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", bearer(tokenA))
	if response.StatusCode != 529 || !bytes.Equal(body, anthropicError) {
		t.Fatalf("first valid request: status=%d body=%s", response.StatusCode, body)
	}
	if err := api.Revoke(t.Context(), podA, tokenA, testAudience); err != nil {
		t.Fatal(err)
	}
	duplicate := http.Header{"Authorization": {"Bearer " + tokenB, "Bearer " + tokenA}}
	for _, headers := range []http.Header{bearer(tokenA), bearer(tokenB), duplicate} {
		refused, refusedBody := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", headers)
		if refused.StatusCode != http.StatusUnauthorized || refused.Header.Get("WWW-Authenticate") != "Bearer" {
			t.Errorf("refusal: status=%d WWW-Authenticate=%q body=%s",
				refused.StatusCode, refused.Header.Get("WWW-Authenticate"), refusedBody)
		}
	}
	if got := len(backend.snapshotRequests()); got != 1 {
		t.Fatalf("backend saw %d requests, want only the first valid request", got)
	}
	if got := reviews.Calls.Load(); got != 3 {
		t.Fatalf("TokenReview count = %d, want every single-token request including revoked token", got)
	}
}

func TestBearerSchemeIsCaseInsensitiveAndAcceptsMultipleSpaces(t *testing.T) {
	tokenA := workloadToken(t, "test-sandbox-a")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	headers := http.Header{"Authorization": {"bearer   " + tokenA}}
	response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", headers)
	got := backend.snapshotRequests()
	if response.StatusCode != 529 || !bytes.Equal(body, anthropicError) ||
		len(got) != 1 || got[0].headers.Get("Authorization") != "Bearer "+testBackendKey {
		t.Fatalf("status=%d body=%s requests=%+v", response.StatusCode, body, got)
	}
}

func TestDependencyOutagesAreSanitizedAndDoNotForwardWorkloadCredentials(t *testing.T) {
	t.Run("TokenReview", func(t *testing.T) {
		tokenA := workloadToken(t, "test-sandbox-a")
		backend := newFakeLiteLLM()
		backendServer := newBackendServer(t, backend)
		ingress := newIngressTestServer(t, kubeapiserver.UnreachableReviews{Err: errors.New("TokenReview unavailable; secret=" + tokenA)}, backendServer.URL)
		client := testClient()
		defer client.CloseIdleConnections()

		response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", bearer(tokenA))
		if response.StatusCode != http.StatusInternalServerError || len(backend.snapshotRequests()) != 0 ||
			bytes.Contains(body, []byte(tokenA)) || bytes.Contains(body, []byte(testBackendKey)) {
			t.Fatalf("status=%d body=%s backend requests=%d", response.StatusCode, body, len(backend.snapshotRequests()))
		}
	})
	t.Run("LiteLLM", func(t *testing.T) {
		tokenA := workloadToken(t, "test-sandbox-a")
		backend := newFakeLiteLLM()
		backendServer := newBackendServer(t, backend)
		backendURL := backendServer.URL
		backendServer.Close()
		ingress := newIngressTestServer(t, api.TokenReviews(), backendURL)
		client := testClient()
		defer client.CloseIdleConnections()

		response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", bearer(tokenA))
		if response.StatusCode != http.StatusBadGateway || len(backend.snapshotRequests()) != 0 ||
			bytes.Contains(body, []byte(tokenA)) || bytes.Contains(body, []byte(testBackendKey)) {
			t.Fatalf("status=%d body=%s backend requests=%d", response.StatusCode, body, len(backend.snapshotRequests()))
		}
	})
}

func TestRealBinaryStartsWithYAMLAndEnvironmentConfiguration(t *testing.T) {
	binary, err := runfiles.Rlocation("ducktape/agentplane/llm_ingress/server_/server")
	if err != nil {
		t.Fatal(err)
	}
	// The token is only good for the environment's audience, so the API server accepting it shows
	// the environment overrode the YAML audience.
	_, tokenA := workload(t, "test-sandbox-a", "test-environment-audience")
	target, _, err := rest.DefaultServerUrlFor(api.Config)
	if err != nil {
		t.Fatal(err)
	}
	transport, err := rest.TransportFor(api.Config)
	if err != nil {
		t.Fatal(err)
	}
	// A spy in front of the API server, so the binary's kubeconfig needs no credentials and the
	// test can count the reviews it sends.
	var reviews atomic.Int32
	apiServer := httptest.NewServer(&httputil.ReverseProxy{
		Rewrite: func(proxied *httputil.ProxyRequest) {
			if proxied.In.Method == http.MethodPost && proxied.In.URL.Path == "/apis/authentication.k8s.io/v1/tokenreviews" {
				reviews.Add(1)
			}
			proxied.SetURL(target)
		},
		Transport: transport,
	})
	defer apiServer.Close()

	var backendRequests int
	var backendMu sync.Mutex
	backendServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_, _ = io.Copy(io.Discard, request.Body)
		backendMu.Lock()
		backendRequests++
		backendMu.Unlock()
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(529)
		_, _ = writer.Write(anthropicError)
	}))
	defer backendServer.Close()

	directory := t.TempDir()
	kubeconfig := filepath.Join(directory, "kubeconfig.yaml")
	kubeconfigYAML := "apiVersion: v1\nkind: Config\n" +
		"clusters:\n- name: test\n  cluster:\n    server: " + apiServer.URL + "\n" +
		"contexts:\n- name: test\n  context:\n    cluster: test\n    user: test\n" +
		"current-context: test\nusers:\n- name: test\n  user: {}\n"
	if err := os.WriteFile(kubeconfig, []byte(kubeconfigYAML), 0o600); err != nil {
		t.Fatal(err)
	}
	settings := filepath.Join(directory, "settings.yaml")
	settingsYAML := "allowed_service_account_namespaces: [" + testNamespace + "]\n" +
		"token_audience: test-yaml-audience\n" +
		"models:\n  - model: configured/model\n    total_context_budget_tokens: 8192\n" +
		"log_levels:\n  root: INFO\n"
	if err := os.WriteFile(settings, []byte(settingsYAML), 0o600); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()

	var logs bytes.Buffer
	command := exec.Command(binary)
	command.Env = []string{
		"OTEL_SDK_DISABLED=true",
		configFileEnv + "=" + settings,
		"AGENTPLANE_LLM_INGRESS_LITELLM_KEY=" + testBackendKey,
		"AGENTPLANE_LLM_INGRESS_HOST=127.0.0.1",
		"AGENTPLANE_LLM_INGRESS_PORT=" + strconv.Itoa(port),
		"AGENTPLANE_LLM_INGRESS_KUBECONFIG=" + kubeconfig,
		"AGENTPLANE_LLM_INGRESS_TOKEN_AUDIENCE=test-environment-audience",
		"AGENTPLANE_LLM_INGRESS_LITELLM_URL=" + backendServer.URL,
	}
	command.Stdout = &logs
	command.Stderr = &logs
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	waited := make(chan error, 1)
	go func() { waited <- command.Wait() }()
	stopped := false
	stop := func() error {
		if stopped {
			return nil
		}
		stopped = true
		_ = command.Process.Signal(os.Interrupt)
		select {
		case err := <-waited:
			return err
		case <-time.After(5 * time.Second):
			_ = command.Process.Kill()
			return <-waited
		}
	}
	defer func() {
		if err := stop(); err != nil {
			t.Errorf("ingress binary exited during shutdown: %v; logs: %s", err, logs.String())
		}
	}()

	client := testClient()
	defer client.CloseIdleConnections()
	baseURL := "http://127.0.0.1:" + strconv.Itoa(port)
	deadline := time.Now().Add(10 * time.Second)
	for {
		response, err := client.Get(baseURL + "/healthz")
		if err == nil {
			_ = response.Body.Close()
			if response.StatusCode == http.StatusOK {
				break
			}
		}
		select {
		case processErr := <-waited:
			stopped = true
			t.Fatalf("binary exited before becoming healthy: %v; logs: %s", processErr, logs.String())
		default:
		}
		if time.Now().After(deadline) {
			processErr := stop()
			t.Fatalf("binary did not become healthy (exit %v); logs: %s", processErr, logs.String())
		}
		time.Sleep(25 * time.Millisecond)
	}

	modelResponse, modelBody := request(t, client, http.MethodGet,
		baseURL+"/agentplane/model-config?model=configured%2Fmodel", "", bearer(tokenA))
	if modelResponse.StatusCode != http.StatusOK || string(modelBody) != "{\"model\":\"configured/model\",\"total_context_budget_tokens\":8192}\n" {
		t.Fatalf("configured model response: status=%d body=%s", modelResponse.StatusCode, modelBody)
	}
	messageResponse, messageBody := request(t, client, http.MethodPost, baseURL+"/v1/messages",
		`{"messages":[]}`, bearer(tokenA))
	if messageResponse.StatusCode != 529 || !bytes.Equal(messageBody, anthropicError) {
		t.Fatalf("binary proxy response: status=%d body=%s", messageResponse.StatusCode, messageBody)
	}
	// Both responses are in, so a second review would already have been counted.
	if got := reviews.Load(); got != 1 {
		t.Fatalf("TokenReview calls = %d, want one shared by both requests", got)
	}
	backendMu.Lock()
	gotBackendRequests := backendRequests
	backendMu.Unlock()
	if gotBackendRequests != 1 {
		t.Fatalf("backend requests=%d, want 1", gotBackendRequests)
	}
	if err := stop(); err != nil {
		t.Fatalf("ingress binary did not shut down cleanly: %v; logs: %s", err, logs.String())
	}
	if strings.Contains(logs.String(), tokenA) || strings.Contains(logs.String(), testBackendKey) {
		t.Fatal("binary logs contained a workload token or LiteLLM key")
	}
}

func TestCentralSessionAttributionRequiresTrustedProducer(t *testing.T) {
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	defer backendServer.Close()
	server := newConfiguredIngressTestServer(t, api.TokenReviews(), config{
		LiteLLMURL: backendServer.URL, LiteLLMKey: testBackendKey,
		CentralSamplerAccounts: []string{testNamespace + "/trusted-sampler"},
	})
	const sessionID = "00000000-0000-4000-8000-000000000055"
	for _, test := range []struct {
		name, id string
		want     int
	}{
		{"ordinary-runner", sessionID, http.StatusForbidden},
		{"trusted-sampler", "not-a-session", http.StatusBadRequest},
		{"trusted-sampler", sessionID, http.StatusOK},
	} {
		token := workloadToken(t, test.name)
		request, err := http.NewRequest(http.MethodPost, server.URL+"/v1/responses", strings.NewReader(`{"model":"test"}`))
		if err != nil {
			t.Fatal(err)
		}
		request.Header.Set("Authorization", "Bearer "+token)
		request.Header.Set("x-agentplane-session-id", test.id)
		request.Header.Set("x-litellm-spend-logs-metadata", `{"agentplane.session_id":"forged"}`)
		request.Header.Set("baggage", "litellm.metadata.spend_logs_metadata.agentplane.session_id=forged,untrusted=value")
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
		if response.StatusCode != test.want {
			t.Fatalf("%s: status %d want %d", test.name, response.StatusCode, test.want)
		}
	}
	requests := backend.snapshotRequests()
	if len(requests) != 1 {
		t.Fatalf("forwarded %d requests", len(requests))
	}
	var members map[string]string
	if err := json.Unmarshal([]byte(requests[0].headers.Get("x-litellm-spend-logs-metadata")), &members); err != nil {
		t.Fatal(err)
	}
	if members["agentplane.session_id"] != sessionID || members["agentplane.service_account"] != "trusted-sampler" || requests[0].headers.Get("x-agentplane-session-id") != "" {
		t.Fatalf("unexpected trusted attribution %v", members)
	}
	verifiedBaggage, err := baggage.Parse(requests[0].headers.Get("baggage"))
	if err != nil {
		t.Fatal(err)
	}
	if verifiedBaggage.Member("litellm.metadata.spend_logs_metadata.agentplane.session_id").Value() != sessionID || verifiedBaggage.Member("untrusted").Value() != "" {
		t.Fatalf("unexpected Session baggage %v", verifiedBaggage)
	}
}

func TestLargeRequestBodyStreamsWithoutFullBuffering(t *testing.T) {
	tokenA := workloadToken(t, "test-sandbox-a")
	firstChunk := bytes.Repeat([]byte("x"), 64*1024)
	received := make(chan struct{})
	var total atomic.Int64
	backend := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		buffer := make([]byte, 32*1024)
		signaled := false
		for {
			n, err := request.Body.Read(buffer)
			total.Add(int64(n))
			if n > 0 && !signaled {
				signaled = true
				close(received)
			}
			if err != nil {
				break
			}
		}
		_, _ = io.WriteString(writer, strconv.FormatInt(total.Load(), 10))
	}))
	t.Cleanup(backend.Close)
	ingress := newIngressTestServer(t, api.TokenReviews(), backend.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	pipeReader, pipeWriter := io.Pipe()
	req, err := http.NewRequest(http.MethodPost, ingress.URL+"/upload", pipeReader)
	if err != nil {
		t.Fatal(err)
	}
	req.Header = bearer(tokenA)
	type result struct {
		body []byte
		err  error
	}
	done := make(chan result, 1)
	go func() {
		response, err := client.Do(req)
		if err != nil {
			done <- result{err: err}
			return
		}
		defer response.Body.Close()
		body, err := io.ReadAll(response.Body)
		done <- result{body: body, err: err}
	}()
	if _, err := pipeWriter.Write(firstChunk); err != nil {
		t.Fatal(err)
	}
	// The request body is still open: only a streaming proxy has delivered bytes to the backend.
	select {
	case <-received:
	case <-time.After(5 * time.Second):
		t.Fatal("backend received nothing while the request body was still open")
	}
	if _, err := pipeWriter.Write(firstChunk); err != nil {
		t.Fatal(err)
	}
	_ = pipeWriter.Close()
	got := <-done
	if got.err != nil || string(got.body) != strconv.Itoa(2*len(firstChunk)) {
		t.Fatalf("body=%q err=%v", got.body, got.err)
	}
}

func TestProtocolUpgradeIsNotForwardedToBackend(t *testing.T) {
	tokenA := workloadToken(t, "test-sandbox-a")
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, api.TokenReviews(), backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	headers := bearer(tokenA)
	headers.Set("Connection", "Upgrade")
	headers.Set("Upgrade", "websocket")
	response, err := client.Do(mustRequest(t, http.MethodGet, ingress.URL+"/v1/models", headers))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, response.Body)
	_ = response.Body.Close()
	got := backend.snapshotRequests()
	if response.StatusCode == http.StatusSwitchingProtocols || len(got) != 1 ||
		got[0].headers.Get("Upgrade") != "" || got[0].headers.Get("Connection") != "" {
		t.Fatalf("status=%d backend requests=%+v", response.StatusCode, got)
	}
}
