package centralsampler

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"

	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/settings"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"google.golang.org/protobuf/proto"
)

// The sampler delegates to Agentplane's LLM ingress for a Session the Dispatcher recorded as
// executing as an Identity, and otherwise calls as itself with the Session claimed by header, as it
// does on every bearer connection.
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

	for name, test := range map[string]struct {
		connection samplersettings.ConnectionConfig
		identity   *identitypb.Identity
	}{
		"a Session without an Identity on the ingress": {connection(samplersettings.AuthTypeAgentplaneIngress), nil},
		"a bearer connection":                          {connection(samplersettings.AuthTypeBearer), identity},
	} {
		t.Run(name, func(t *testing.T) {
			authorization, headers, err := providerAuthorization(test.connection, providerAttribution{SessionID: sessionID, Identity: test.identity})
			if err != nil {
				t.Fatal(err)
			}
			if authorization != "Bearer test-workload-token" || headers["x-agentplane-session-id"] != sessionID || headers[auth.DelegationMetadataKey] != "" {
				t.Fatalf("direct call: authorization %q headers %v", authorization, headers)
			}
		})
	}
}
