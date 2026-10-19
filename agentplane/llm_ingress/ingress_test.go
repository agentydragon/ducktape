package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/bazelbuild/rules_go/go/runfiles"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	testAudience           = "llm-ingress-test-audience"
	testNamespace          = "sandbox-namespace"
	testTokenA             = "opaque-workload-token-a"
	testTokenB             = "opaque-workload-token-b"
	testWrongAudienceToken = "opaque-wrong-audience-token"
	testBackendKey         = "sk-server-held-test-key"
)

var toolStream = []byte(
	"event: response.output_item.added\n" +
		"data: {\"type\":\"response.output_item.added\",\"item\":{\"type\":\"function_call\",\"name\":\"shell\",\"call_id\":\"call_1\"}}\n\n" +
		"event: response.function_call_arguments.delta\n" +
		"data: {\"type\":\"response.function_call_arguments.delta\",\"delta\":\"{\\\"command\\\":\\\"pwd\\\"}\"}\n\n" +
		"event: response.completed\n" +
		"data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}\n\n",
)
var anthropicError = []byte(`{"type":"error","error":{"type":"overloaded_error","message":"backend busy"}}`)

type reviewOutcome struct {
	status authenticationv1.TokenReviewStatus
	err    error
}

// fakeTokenReviews returns explicit typed TokenReview results. It tests how ingress interprets
// the response; it does not model Kubernetes token validation or Pod-binding semantics.
type fakeTokenReviews struct {
	mu       sync.Mutex
	outcomes map[string]reviewOutcome
	requests []authenticationv1.TokenReviewSpec
}

func newFakeTokenReviews() *fakeTokenReviews {
	return &fakeTokenReviews{outcomes: make(map[string]reviewOutcome)}
}

func (f *fakeTokenReviews) set(token string, status authenticationv1.TokenReviewStatus) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.outcomes[token] = reviewOutcome{status: *status.DeepCopy()}
}

func (f *fakeTokenReviews) fail(token string, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.outcomes[token] = reviewOutcome{err: err}
}

func (f *fakeTokenReviews) revoke(token string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.outcomes, token)
}

func (f *fakeTokenReviews) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = append(f.requests, *review.Spec.DeepCopy())
	outcome, found := f.outcomes[review.Spec.Token]
	if !found {
		return &authenticationv1.TokenReview{}, nil
	}
	if outcome.err != nil {
		return nil, outcome.err
	}
	return &authenticationv1.TokenReview{Status: *outcome.status.DeepCopy()}, nil
}

func (f *fakeTokenReviews) snapshotRequests() []authenticationv1.TokenReviewSpec {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]authenticationv1.TokenReviewSpec(nil), f.requests...)
}

func acceptedReview(namespace, account, podName, podUID string, audiences ...string) authenticationv1.TokenReviewStatus {
	return authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     audiences,
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:" + namespace + ":" + account,
			UID:      "service-account-uid",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {podName},
				"authentication.kubernetes.io/pod-uid":  {podUID},
			},
		},
	}
}

func newIngressTestServer(t *testing.T, reviews auth.TokenReviews, backendURL string, models ...modelConfig) *httptest.Server {
	t.Helper()
	resolver, err := auth.NewResolver(reviews, testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	handler, err := newIngressHandler(config{
		LiteLLMURL: backendURL,
		LiteLLMKey: testBackendKey,
		Models:     models,
	}, resolver, slog.New(slog.NewTextHandler(io.Discard, nil)))
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
	reviews := newFakeTokenReviews()
	reviews.set(testTokenA, acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", testAudience))
	reviews.set(testTokenB, acceptedReview(testNamespace, "sandbox-b", "sandbox-b", "pod-b-uid", testAudience))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	body := `{"model":"chatgpt/oai-responses/gpt-5.6-luna","stream":true,"metadata":{"sandbox_name":"forged"}}`
	firstHeaders := bearer(testTokenA)
	firstHeaders.Set("Content-Type", "application/json")
	firstHeaders.Set("x-litellm-spend-logs-metadata", `{"agentplane.sandbox_name":"forged"}`)
	firstHeaders.Set("x-litellm-customer-id", "forged")
	firstHeaders.Set("x-api-key", "forged")
	firstHeaders.Set("x-litellm-api-key", "forged")
	for _, name := range []string{"x-agentplane-pod-uid", "x-sandbox-name", "x-pod-uid", "x-agent-id", "x-thread-id"} {
		firstHeaders.Set(name, "forged")
	}
	first, firstBody := request(t, client, http.MethodPost, ingress.URL+"/v1/responses?trace=kept", body, firstHeaders)
	second, secondBody := request(t, client, http.MethodPost, ingress.URL+"/v1/responses?trace=kept", body, bearer(testTokenB))
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
		"agentplane.service_account": "sandbox-a",
		"agentplane.pod_name":        "sandbox-a",
		"agentplane.pod_uid":         "pod-a-uid",
	}
	if !maps.Equal(firstMetadata, wantFirst) || secondMetadata["agentplane.pod_name"] != "sandbox-b" ||
		secondMetadata["agentplane.pod_uid"] == firstMetadata["agentplane.pod_uid"] {
		t.Fatalf("verified metadata: first=%v second=%v", firstMetadata, secondMetadata)
	}
	for _, transcript := range [][]byte{firstBody, secondBody} {
		if bytes.Contains(transcript, []byte(testTokenA)) || bytes.Contains(transcript, []byte(testTokenB)) ||
			bytes.Contains(transcript, []byte(testBackendKey)) {
			t.Fatal("a credential appeared in a caller response")
		}
	}
}

func TestAnthropicStatusErrorBodyAndRequestShapePassUnchanged(t *testing.T) {
	reviews := newFakeTokenReviews()
	reviews.set(testTokenA, acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", testAudience))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	body := `{"model":"anthropic-api/ant-messages/claude-haiku-4-5-20251001","max_tokens":32,"messages":[]}`
	response, gotBody := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", body, bearer(testTokenA))
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
	reviews := newFakeTokenReviews()
	reviews.set(testTokenA, acceptedReview(testNamespace, "live", "live", "live-pod-uid", testAudience))
	reviews.set(testWrongAudienceToken, acceptedReview(testNamespace, "live", "live", "live-pod-uid", "somewhere-else"))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	attempts := []*http.Response{
		doRequest(t, client, ingress.URL+"/v1/messages", http.Header{}),
		doRequest(t, client, ingress.URL+"/v1/messages", http.Header{"Authorization": {"not-a-bearer"}}),
		doRequest(t, client, ingress.URL+"/v1/messages", bearer("unknown-token")),
		doRequest(t, client, ingress.URL+"/v1/messages", bearer(testWrongAudienceToken)),
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
	if got := len(reviews.snapshotRequests()); got != 2 {
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
	reviews := newFakeTokenReviews()
	reviews.set("unowned-token", acceptedReview(testNamespace, "unowned", "unowned", "unowned-pod-uid", testAudience))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/responses",
		`{"model":"chatgpt/oai-responses/gpt-5.6-luna","stream":true}`, bearer("unowned-token"))
	if response.StatusCode != http.StatusOK || !bytes.Equal(body, toolStream) {
		t.Fatalf("status=%d body=%s", response.StatusCode, body)
	}
	got := backend.snapshotRequests()
	var metadata map[string]string
	if len(got) != 1 || json.Unmarshal([]byte(got[0].headers.Get("x-litellm-spend-logs-metadata")), &metadata) != nil ||
		metadata["agentplane.pod_name"] != "unowned" || metadata["agentplane.service_account"] != "unowned" {
		t.Fatalf("metadata did not follow verified service account: %v", metadata)
	}
}

func TestModelConfigLookupIsAuthenticatedAndAnsweredFromConfiguration(t *testing.T) {
	const model = "ollama/oai-chat/qwen3.8-flash-next-iq4xs-256k"
	reviews := newFakeTokenReviews()
	reviews.set(testTokenA, acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", testAudience))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL, modelConfig{
		Model: model, TotalContextBudgetTokens: 256 * 1024,
	})
	client := testClient()
	defer client.CloseIdleConnections()

	found, foundBody := request(t, client, http.MethodGet, ingress.URL+"/agentplane/model-config?model="+model, "", bearer(testTokenA))
	missing, missingBody := request(t, client, http.MethodGet, ingress.URL+"/agentplane/model-config?model=unlisted", "", bearer(testTokenA))
	unauthenticated := doRequest(t, client, ingress.URL+"/agentplane/model-config?model="+model, http.Header{})
	unauthenticatedBody, _ := io.ReadAll(unauthenticated.Body)
	_ = unauthenticated.Body.Close()
	missingParameter, missingParameterBody := request(t, client, http.MethodGet, ingress.URL+"/agentplane/model-config", "", bearer(testTokenA))
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
	reviews := newFakeTokenReviews()
	reviews.set(testTokenA, acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", testAudience))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	req, err := http.NewRequest(http.MethodPost, ingress.URL+"/gated-stream", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header = bearer(testTokenA)
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
	reviews := newFakeTokenReviews()
	reviews.set(testTokenA, acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", testAudience))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	headers := bearer(testTokenA)
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

func TestHopByHopAndCredentialHeadersAreFilteredByCopyHelpers(t *testing.T) {
	headers := http.Header{
		"Connection":                    {"keep-alive, x-drop"},
		"Keep-Alive":                    {"timeout=5"},
		"X-Drop":                        {"untrusted-hop-value"},
		"Authorization":                 {"Bearer workload-token"},
		"X-Api-Key":                     {"forged"},
		"X-Litellm-Api-Key":             {"forged"},
		"X-Litellm-Spend-Logs-Metadata": {`{"forged":true}`},
		"X-Agentplane-Pod-Uid":          {"forged"},
		"Content-Length":                {"8"},
		"X-Ordinary":                    {"kept"},
	}
	principal := auth.Principal{
		Account: auth.Account{Namespace: testNamespace, Name: "sandbox-a"},
		PodName: "sandbox-a", PodUID: "pod-a-uid",
	}
	forwarded := forwardedRequestHeaders(headers, principal, testBackendKey)
	for _, name := range []string{
		"Connection", "Keep-Alive", "x-drop", "X-Api-Key", "X-Litellm-Api-Key",
		"X-Agentplane-Pod-Uid", "Content-Length",
	} {
		if forwarded.Get(name) != "" {
			t.Errorf("request header %q was retained: %v", name, forwarded)
		}
	}
	if forwarded.Get("Authorization") != "Bearer "+testBackendKey || forwarded.Get("X-Ordinary") != "kept" {
		t.Fatalf("request headers were not rebuilt safely: %v", forwarded)
	}
	var metadata map[string]string
	if err := json.Unmarshal([]byte(forwarded.Get("X-Litellm-Spend-Logs-Metadata")), &metadata); err != nil ||
		metadata["agentplane.pod_uid"] != "pod-a-uid" {
		t.Fatalf("trusted metadata was not stamped: %v, err=%v", metadata, err)
	}

	source := http.Header{
		"Connection":       {"x-drop"},
		"X-Drop":           {"backend-hop"},
		"Authorization":    {"Bearer backend-key"},
		"Content-Length":   {"999"},
		"Set-Cookie":       {"first=1", "second=2"},
		"Content-Encoding": {"gzip"},
		"X-Ordinary":       {"kept"},
	}
	destination := make(http.Header)
	copyResponseHeaders(destination, source)
	if destination.Get("X-Drop") != "" || destination.Get("Authorization") != "" || destination.Get("Content-Length") != "" ||
		destination.Get("Content-Encoding") != "gzip" || destination.Get("X-Ordinary") != "kept" ||
		len(destination.Values("Set-Cookie")) != 2 {
		t.Fatalf("response headers were not filtered/copied correctly: %v", destination)
	}
}

func TestTruncatedBackendResponseDoesNotLookLikeSuccessfulEndOfStream(t *testing.T) {
	reviews := newFakeTokenReviews()
	reviews.set(testTokenA, acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", testAudience))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	req, err := http.NewRequest(http.MethodPost, ingress.URL+"/truncated", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header = bearer(testTokenA)
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

func TestRevokedAndDisallowedNamespaceTokensFailOnNextRequest(t *testing.T) {
	reviews := newFakeTokenReviews()
	reviews.set(testTokenA, acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", testAudience))
	reviews.set(testTokenB, acceptedReview("other-namespace", "sandbox-b", "sandbox-b", "pod-b", testAudience))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", bearer(testTokenA))
	if response.StatusCode != 529 || !bytes.Equal(body, anthropicError) {
		t.Fatalf("first valid request: status=%d body=%s", response.StatusCode, body)
	}
	reviews.revoke(testTokenA)
	duplicate := http.Header{"Authorization": {"Bearer " + testTokenB, "Bearer " + testTokenA}}
	for _, headers := range []http.Header{bearer(testTokenA), bearer(testTokenB), duplicate} {
		refused, refusedBody := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", headers)
		if refused.StatusCode != http.StatusUnauthorized || refused.Header.Get("WWW-Authenticate") != "Bearer" {
			t.Errorf("refusal: status=%d WWW-Authenticate=%q body=%s",
				refused.StatusCode, refused.Header.Get("WWW-Authenticate"), refusedBody)
		}
	}
	if got := len(backend.snapshotRequests()); got != 1 {
		t.Fatalf("backend saw %d requests, want only the first valid request", got)
	}
	if got := len(reviews.snapshotRequests()); got != 3 {
		t.Fatalf("TokenReview count = %d, want every single-token request including revoked token", got)
	}
}

func TestBearerSchemeIsCaseInsensitiveAndAcceptsMultipleSpaces(t *testing.T) {
	reviews := newFakeTokenReviews()
	reviews.set(testTokenA, acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", testAudience))
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	ingress := newIngressTestServer(t, reviews, backendServer.URL)
	client := testClient()
	defer client.CloseIdleConnections()

	headers := http.Header{"Authorization": {"bearer   " + testTokenA}}
	response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", headers)
	got := backend.snapshotRequests()
	if response.StatusCode != 529 || !bytes.Equal(body, anthropicError) ||
		len(got) != 1 || got[0].headers.Get("Authorization") != "Bearer "+testBackendKey {
		t.Fatalf("status=%d body=%s requests=%+v", response.StatusCode, body, got)
	}
}

func TestDependencyOutagesAreSanitizedAndDoNotForwardWorkloadCredentials(t *testing.T) {
	t.Run("TokenReview", func(t *testing.T) {
		reviews := newFakeTokenReviews()
		reviews.fail(testTokenA, errors.New("TokenReview unavailable; secret="+testTokenA))
		backend := newFakeLiteLLM()
		backendServer := newBackendServer(t, backend)
		ingress := newIngressTestServer(t, reviews, backendServer.URL)
		client := testClient()
		defer client.CloseIdleConnections()

		response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", bearer(testTokenA))
		if response.StatusCode != http.StatusInternalServerError || len(backend.snapshotRequests()) != 0 ||
			bytes.Contains(body, []byte(testTokenA)) || bytes.Contains(body, []byte(testBackendKey)) {
			t.Fatalf("status=%d body=%s backend requests=%d", response.StatusCode, body, len(backend.snapshotRequests()))
		}
	})
	t.Run("LiteLLM", func(t *testing.T) {
		reviews := newFakeTokenReviews()
		reviews.set(testTokenA, acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", testAudience))
		backend := newFakeLiteLLM()
		backendServer := newBackendServer(t, backend)
		backendURL := backendServer.URL
		backendServer.Close()
		ingress := newIngressTestServer(t, reviews, backendURL)
		client := testClient()
		defer client.CloseIdleConnections()

		response, body := request(t, client, http.MethodPost, ingress.URL+"/v1/messages", "", bearer(testTokenA))
		if response.StatusCode != http.StatusBadGateway || len(backend.snapshotRequests()) != 0 ||
			bytes.Contains(body, []byte(testTokenA)) || bytes.Contains(body, []byte(testBackendKey)) {
			t.Fatalf("status=%d body=%s backend requests=%d", response.StatusCode, body, len(backend.snapshotRequests()))
		}
	})
}

func TestRealBinaryStartsWithYAMLFlagsAndEnvironmentConfiguration(t *testing.T) {
	binary, err := runfiles.Rlocation("ducktape/agentplane/llm_ingress/server_/server")
	if err != nil {
		t.Fatal(err)
	}
	reviewsSeen := make(chan authenticationv1.TokenReviewSpec, 4)
	apiServer := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/apis/authentication.k8s.io/v1/tokenreviews" {
			http.NotFound(writer, request)
			return
		}
		var incoming authenticationv1.TokenReview
		if err := json.NewDecoder(request.Body).Decode(&incoming); err != nil {
			http.Error(writer, "invalid TokenReview request", http.StatusBadRequest)
			return
		}
		reviewsSeen <- *incoming.Spec.DeepCopy()
		// This narrow HTTP fixture verifies client-go wiring only; it does not prove Kubernetes
		// validates token signatures, audiences, revocation or Pod binding.
		result := &authenticationv1.TokenReview{
			TypeMeta: metav1.TypeMeta{APIVersion: "authentication.k8s.io/v1", Kind: "TokenReview"},
			Status:   acceptedReview(testNamespace, "sandbox-a", "sandbox-a", "pod-a-uid", "flag-audience"),
		}
		writer.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(writer).Encode(result)
	}))
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
	command := exec.Command(
		binary,
		"--host=127.0.0.1",
		"--port="+strconv.Itoa(port),
		"--kubeconfig="+kubeconfig,
		"--token-audience=flag-audience",
		"--litellm-url="+backendServer.URL,
	)
	command.Env = []string{
		configFileEnv + "=" + settings,
		"AGENTPLANE_LLM_INGRESS_LITELLM_KEY=" + testBackendKey,
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
		baseURL+"/agentplane/model-config?model=configured%2Fmodel", "", bearer(testTokenA))
	if modelResponse.StatusCode != http.StatusOK || string(modelBody) != "{\"model\":\"configured/model\",\"total_context_budget_tokens\":8192}\n" {
		t.Fatalf("configured model response: status=%d body=%s", modelResponse.StatusCode, modelBody)
	}
	messageResponse, messageBody := request(t, client, http.MethodPost, baseURL+"/v1/messages",
		`{"messages":[]}`, bearer(testTokenA))
	if messageResponse.StatusCode != 529 || !bytes.Equal(messageBody, anthropicError) {
		t.Fatalf("binary proxy response: status=%d body=%s", messageResponse.StatusCode, messageBody)
	}
	for i := 0; i < 2; i++ {
		select {
		case spec := <-reviewsSeen:
			if spec.Token != testTokenA || len(spec.Audiences) != 1 || spec.Audiences[0] != "flag-audience" {
				t.Fatalf("TokenReview spec = %+v", spec)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("binary did not send TokenReview")
		}
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
	if strings.Contains(logs.String(), testTokenA) || strings.Contains(logs.String(), testBackendKey) {
		t.Fatal("binary logs contained a workload token or LiteLLM key")
	}
}
