package server

import (
	"encoding/json"
	"io"
	"log/slog"
	"maps"
	"net/http"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/baggage"
)

// delegatedHeaders are a central sampler's headers for one inference call it makes for an Identity.
func delegatedHeaders(t *testing.T, identity auth.Identity, provenance *delegationpb.Delegation) http.Header {
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
		auth.DelegationMetadataKey:      {encoded},
		"x-litellm-spend-logs-metadata": {`{"agentplane.identity":"forged","agentplane.session_id":"forged"}`},
		"baggage":                       {"litellm.metadata.spend_logs_metadata.agentplane.identity=forged,untrusted=value"},
	}
}

func sessionProvenance(sessionID string) *delegationpb.Delegation {
	return &delegationpb.Delegation{Provenance: &delegationpb.Delegation_Session{Session: &delegationpb.SessionProvenance{SessionId: sessionID}}}
}

// A service actor proves only its own service identity. Attribution comes from the
// live effective Identity and Session envelope; the actor supplies no Pod claims.
func TestServiceSamplerDelegationOverMTLS(t *testing.T) {
	backend := newFakeLiteLLM()
	backendServer := newBackendServer(t, backend)
	const policyNamespace = "test-service-ingress-policies"
	named := workloadauthtesting.NewIdentityWorkload(t, api, testNamespace, "service-sampled-owner", policyNamespace)
	identity := auth.Identity{Name: named.Identity.Name, UID: string(named.Identity.UID)}
	identities, err := auth.KubernetesIdentityUIDs(named.Objects, policyNamespace)
	if err != nil {
		t.Fatal(err)
	}
	callers, err := auth.NewSVIDResolver(named.Objects, []string{testNamespace}, &resolver.Identities{PolicyNamespace: policyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	c := config{LiteLLMURL: backendServer.URL, LiteLLMKey: testBackendKey, Attribution: defaultAttribution(), CentralSamplerIdentityCallers: true, CentralSamplerServices: []string{peertls.CentralSamplerURI}}
	handler, err := newIngressHandler(c, callers, identities, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	server := serveIngressTestHandler(t, handler)
	headers := delegatedHeaders(t, identity, sessionProvenance("00000000-0000-4000-8000-000000000077"))
	headers.Del("Authorization")
	for _, tc := range []struct {
		name, actor string
		bearer      bool
		want        int
	}{
		{name: "allowed sampler", actor: peertls.CentralSamplerURI, want: http.StatusOK},
		{name: "wrong service", actor: peertls.ActionsURI, want: http.StatusForbidden},
		{name: "no certificate", want: http.StatusUnauthorized},
		{name: "bearer ambiguity", actor: peertls.CentralSamplerURI, bearer: true, want: http.StatusUnauthorized},
	} {
		t.Run(tc.name, func(t *testing.T) {
			callHeaders := headers.Clone()
			if tc.bearer {
				callHeaders.Set("Authorization", "Bearer test-token")
			}
			response, _ := request(t, server.client(t, tc.actor), http.MethodPost, server.URL+"/v1/responses", `{"model":"test"}`, callHeaders)
			if response.StatusCode != tc.want {
				t.Fatalf("status=%d,want=%d", response.StatusCode, tc.want)
			}
		})
	}
	client := server.client(t, peertls.CentralSamplerURI)
	for _, tc := range []struct {
		name    string
		headers http.Header
		want    int
	}{
		{"stale Identity", delegatedHeaders(t, auth.Identity{Name: identity.Name, UID: uuid.NewString()}, sessionProvenance(uuid.NewString())), http.StatusForbidden},
		{"Action provenance", delegatedHeaders(t, identity, &delegationpb.Delegation{Provenance: &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{RequestId: uuid.NewString(), ExecutionId: uuid.NewString()}}}), http.StatusForbidden},
		{"malformed envelope", http.Header{auth.DelegationMetadataKey: {"not base64 %%"}}, http.StatusUnauthorized},
		{"extra Session header", func() http.Header { h := headers.Clone(); h.Set("x-agentplane-session-id", uuid.NewString()); return h }(), http.StatusForbidden},
	} {
		t.Run(tc.name, func(t *testing.T) {
			response, _ := request(t, client, http.MethodPost, server.URL+"/v1/responses", `{"model":"test"}`, tc.headers)
			if response.StatusCode != tc.want {
				t.Fatalf("status=%d,want=%d", response.StatusCode, tc.want)
			}
		})
	}
	// The same verified service receives no delegated grant when it is not configured.
	c.CentralSamplerIdentityCallers = false
	c.CentralSamplerServices = nil
	ungranted, err := newIngressHandler(c, callers, nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	ungrantedServer := serveIngressTestHandler(t, ungranted)
	response, _ := request(t, ungrantedServer.client(t, peertls.CentralSamplerURI), http.MethodPost, ungrantedServer.URL+"/v1/responses", `{}`, headers)
	if response.StatusCode != http.StatusForbidden {
		t.Fatalf("ungranted actor status=%d, want forbidden", response.StatusCode)
	}
	forwarded := backend.snapshotRequests()
	if len(forwarded) != 1 {
		t.Fatalf("forwarded=%d", len(forwarded))
	}
	var claims map[string]string
	if err := json.Unmarshal([]byte(forwarded[0].headers.Get("x-litellm-spend-logs-metadata")), &claims); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"agentplane.identity": identity.Name, "agentplane.identity_uid": identity.UID, "agentplane.session_id": "00000000-0000-4000-8000-000000000077"}
	if !maps.Equal(claims, want) {
		t.Fatalf("claims=%v,want=%v", claims, want)
	}
	if forwarded[0].headers.Get(auth.DelegationMetadataKey) != "" || forwarded[0].headers.Get("Authorization") != "Bearer "+testBackendKey {
		t.Fatalf("actor credential or envelope forwarded: %v", forwarded[0].headers)
	}
	verifiedBaggage, err := baggage.Parse(forwarded[0].headers.Get("baggage"))
	if err != nil {
		t.Fatal(err)
	}
	if verifiedBaggage.Member("litellm.metadata.spend_logs_metadata.agentplane.session_id").Value() != want["agentplane.session_id"] || verifiedBaggage.Member("untrusted").Value() != "" {
		t.Fatalf("unexpected delegated baggage %v", verifiedBaggage)
	}
}
