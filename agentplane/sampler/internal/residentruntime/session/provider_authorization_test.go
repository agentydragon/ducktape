package session

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"google.golang.org/protobuf/proto"
)

// The sampler delegates to Agentplane's LLM ingress for the execution Identity. Direct bearer
// connections are never delegated to.
func TestProviderAuthorizationDelegatesOnlyForAnIdentityOnTheAgentplaneIngress(t *testing.T) {
	tokenFile := filepath.Join(t.TempDir(), "workload-token")
	if err := os.WriteFile(tokenFile, []byte("test-workload-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	connection := func(kind samplersettings.AuthType) samplersettings.ConnectionConfig {
		c := samplersettings.ConnectionConfig{BaseURL: "https://llm-ingress.test", Auth: samplersettings.AuthConfig{Type: kind, CredentialFile: tokenFile}}
		if err := c.Validate(); err != nil {
			t.Fatalf("%s connection: %v", kind, err)
		}
		return c
	}
	identity := &identitypb.Identity{Name: "test-session-identity", Uid: "test-identity-uid"}
	const sessionID = "00000000-0000-4000-8000-000000000042"

	authorization, headers, err := providerAuthorization(connection(samplersettings.AuthTypeAgentplaneIngress), providerAttribution{SessionID: sessionID, Identity: identity})
	if err != nil {
		t.Fatal(err)
	}
	if authorization != auth.DelegatedScheme+" test-workload-token" || headers["x-agentplane-session-id"] != "" {
		t.Fatalf("delegated call: authorization %q headers %v", authorization, headers)
	}
	encoded, err := base64.StdEncoding.DecodeString(headers[auth.DelegationMetadataKey])
	if err != nil {
		t.Fatal(err)
	}
	envelope := &delegationpb.Delegation{}
	if err := proto.Unmarshal(encoded, envelope); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(envelope.GetIdentity(), identity) || envelope.GetSession().GetSessionId() != sessionID || envelope.GetAction() != nil {
		t.Fatalf("envelope = %v, want %v acting for Session %s", envelope, identity, sessionID)
	}

	if _, _, err := providerAuthorization(connection(samplersettings.AuthTypeAgentplaneIngress), providerAttribution{SessionID: sessionID}); err == nil {
		t.Fatal("Agentplane ingress accepted a Session without its execution Identity")
	}
	authorization, headers, err = providerAuthorization(connection(samplersettings.AuthTypeBearer), providerAttribution{SessionID: sessionID, Identity: identity})
	if err != nil {
		t.Fatal(err)
	}
	if authorization != "Bearer test-workload-token" || headers["x-agentplane-session-id"] != sessionID || headers[auth.DelegationMetadataKey] != "" {
		t.Fatalf("direct call: authorization %q headers %v", authorization, headers)
	}
}

func TestProviderAuthorizationUsesCentralSamplerSVIDAndRetainsTypedDelegation(t *testing.T) {
	connection := samplersettings.ConnectionConfig{
		BaseURL: "https://llm-ingress.test",
		Auth:    samplersettings.AuthConfig{Type: samplersettings.AuthTypeAgentplaneIngress},
		TLS: &peertls.ClientTLS{
			CAFile: "/run/agentplane/ca.pem", ServerURI: peertls.LLMIngressURI,
			CertFile: "/run/agentplane/svid/tls.crt", KeyFile: "/run/agentplane/svid/tls.key", ClientURI: peertls.CentralSamplerURI,
		},
	}
	if err := connection.Validate(); err != nil {
		t.Fatal(err)
	}
	identity := &identitypb.Identity{Name: "test-session-identity", Uid: "test-identity-uid"}
	const sessionID = "00000000-0000-4000-8000-000000000042"

	authorization, headers, err := providerAuthorization(connection, providerAttribution{SessionID: sessionID, Identity: identity})
	if err != nil {
		t.Fatal(err)
	}
	if authorization != "" || headers["x-agentplane-session-id"] != "" {
		t.Fatalf("SVID call: authorization %q headers %v", authorization, headers)
	}
	encoded, err := base64.StdEncoding.DecodeString(headers[auth.DelegationMetadataKey])
	if err != nil {
		t.Fatal(err)
	}
	envelope := &delegationpb.Delegation{}
	if err := proto.Unmarshal(encoded, envelope); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(envelope.GetIdentity(), identity) || envelope.GetSession().GetSessionId() != sessionID || envelope.GetAction() != nil {
		t.Fatalf("SVID delegation envelope = %v, want %v acting for Session %s", envelope, identity, sessionID)
	}
}
