package main

import (
	"encoding/json"
	"io"
	"log/slog"
	"maps"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/baggage"
)

// delegatedHeaders are a central sampler's headers for one inference call it makes for an Identity.
func delegatedHeaders(t *testing.T, token string, identity auth.Identity, provenance *delegationpb.Delegation) http.Header {
	t.Helper()
	envelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: identity.UID}},
		Provenance:      provenance.GetProvenance(),
	}
	encoded, err := auth.EncodeDelegationHeader(envelope)
	if err != nil {
		t.Fatal(err)
	}
	return http.Header{
		"Authorization":                 {auth.DelegatedScheme + " " + token},
		auth.DelegationMetadataKey:      {encoded},
		"x-litellm-spend-logs-metadata": {`{"agentplane.identity":"forged","agentplane.session_id":"forged"}`},
		"baggage":                       {"litellm.metadata.spend_logs_metadata.agentplane.identity=forged,untrusted=value"},
	}
}

func sessionProvenance(sessionID string) *delegationpb.Delegation {
	return &delegationpb.Delegation{Provenance: &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: sessionID}}}
}

// A configured central sampler acting for an Identity is attributed to that Identity, the Pod it
// called from and the Session its envelope names; its own account never reaches LiteLLM, and only a
// live Identity incarnation, a Session profile and the configured grant are served.
func TestADelegatingCentralSamplerIsAttributedToTheIdentityItActsFor(t *testing.T) {
	const policyNamespace = "test-ingress-delegation-policies"
	const sessionID = "00000000-0000-4000-8000-000000000077"
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	named := workloadauthtesting.NewIdentityWorkload(t, api, testNamespace, "test-delegated-identity", policyNamespace)
	identity := auth.Identity{Name: named.Identity.Name, UID: string(named.Identity.UID)}
	identities, err := auth.KubernetesIdentityUIDs(named.Objects, policyNamespace)
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(api.TokenReviews(), testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	c := config{
		LiteLLMURL: backendServer.URL, LiteLLMKey: testBackendKey, Attribution: defaultAttribution(),
		PolicyNamespace: policyNamespace, CentralSamplerAccounts: []string{testNamespace + "/trusted-sampler"},
	}
	ungranted, err := newIngressHandler(c, resolver, nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	ungrantedServer := httptest.NewServer(ungranted)
	t.Cleanup(ungrantedServer.Close)
	c.CentralSamplerIdentityCallers = true
	handler, err := newIngressHandler(c, resolver, identities, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	samplerPod, samplerToken := workload(t, "trusted-sampler", testAudience)
	strangerToken := workloadToken(t, "ordinary-runner")
	client := testClient()

	response, _ := request(t, client, http.MethodPost, server.URL+"/v1/responses", `{"model":"test"}`,
		delegatedHeaders(t, samplerToken, identity, sessionProvenance(sessionID)))
	if response.StatusCode != http.StatusOK {
		t.Fatalf("delegated inference: status %d", response.StatusCode)
	}
	requests := backend.snapshotRequests()
	if len(requests) != 1 {
		t.Fatalf("forwarded %d requests", len(requests))
	}
	forwarded := requests[0].headers
	var members map[string]string
	if err := json.Unmarshal([]byte(forwarded.Get("x-litellm-spend-logs-metadata")), &members); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"agentplane.identity": identity.Name, "agentplane.identity_uid": identity.UID,
		"agentplane.pod_name": samplerPod.Name, "agentplane.pod_uid": string(samplerPod.UID),
		"agentplane.session_id": sessionID,
	}
	if !maps.Equal(members, want) {
		t.Fatalf("delegated attribution = %v, want %v", members, want)
	}
	if forwarded.Get(auth.DelegationMetadataKey) != "" || !strings.HasPrefix(forwarded.Get("Authorization"), "Bearer "+testBackendKey) {
		t.Fatalf("the envelope or the sampler's credential reached LiteLLM: %v", forwarded)
	}
	verifiedBaggage, err := baggage.Parse(forwarded.Get("baggage"))
	if err != nil {
		t.Fatal(err)
	}
	if verifiedBaggage.Member("litellm.metadata.spend_logs_metadata.agentplane.session_id").Value() != sessionID ||
		verifiedBaggage.Member("untrusted").Value() != "" {
		t.Fatalf("unexpected delegated baggage %v", verifiedBaggage)
	}

	for _, refused := range []struct {
		name    string
		url     string
		headers http.Header
		want    int
	}{
		{"an actor without the Identity grant", ungrantedServer.URL, delegatedHeaders(t, samplerToken, identity, sessionProvenance(sessionID)), http.StatusUnauthorized},
		{"an ordinary workload", server.URL, delegatedHeaders(t, strangerToken, identity, sessionProvenance(sessionID)), http.StatusForbidden},
		{"a stale Identity incarnation", server.URL, delegatedHeaders(t, samplerToken, auth.Identity{Name: identity.Name, UID: uuid.NewString()}, sessionProvenance(sessionID)), http.StatusForbidden},
		{"Action provenance", server.URL, delegatedHeaders(t, samplerToken, identity, &delegationpb.Delegation{
			Provenance: &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{RequestId: uuid.NewString(), ExecutionId: uuid.NewString()}},
		}), http.StatusForbidden},
		{"a Session claimed by header beside the envelope", server.URL, func() http.Header {
			headers := delegatedHeaders(t, samplerToken, identity, sessionProvenance(sessionID))
			headers.Set("x-agentplane-session-id", sessionID)
			return headers
		}(), http.StatusForbidden},
		{"a malformed envelope", server.URL, http.Header{
			"Authorization": {auth.DelegatedScheme + " " + samplerToken}, auth.DelegationMetadataKey: {"not base64 %%"},
		}, http.StatusUnauthorized},
	} {
		t.Run(refused.name, func(t *testing.T) {
			response, _ := request(t, client, http.MethodPost, refused.url+"/v1/responses", `{"model":"test"}`, refused.headers)
			if response.StatusCode != refused.want {
				t.Fatalf("status %d, want %d", response.StatusCode, refused.want)
			}
		})
	}
	if forwarded := backend.snapshotRequests(); len(forwarded) != 1 {
		t.Fatalf("a refused delegated call reached LiteLLM: %d requests", len(forwarded))
	}
}
