package environment_runtime_transport_test

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"google.golang.org/grpc/peer"
)

func TestSecurityValidateRequiresKnownSerializedMode(t *testing.T) {
	for name, input := range map[string]string{
		"missing": `{}`,
		"unknown": `{"mode":"plaintext"}`,
	} {
		t.Run(name, func(t *testing.T) {
			var security environment_runtime_transport.Security
			if err := json.Unmarshal([]byte(input), &security); err != nil {
				t.Fatal(err)
			}
			if err := security.Validate(); err == nil {
				t.Fatalf("Validate accepted %s security mode", name)
			}
		})
	}
}

func TestSecurityValidateRejectsMissingAndContradictoryTLS(t *testing.T) {
	cases := map[string]environment_runtime_transport.Security{
		"mtls without TLS": {Mode: environment_runtime_transport.MutualTLS},
		"missing certificate": {Mode: environment_runtime_transport.MutualTLS, TLS: &environment_runtime_transport.Config{
			KeyFile: "/etc/runner/tls.key",
			CAFile:  "/etc/runner/ca.pem",
		}},
		"relative CA path": {Mode: environment_runtime_transport.MutualTLS, TLS: &environment_runtime_transport.Config{
			CertFile: "/etc/runner/tls.crt",
			KeyFile:  "/etc/runner/tls.key",
			CAFile:   "ca.pem",
		}},
		"insecure with TLS": {Mode: environment_runtime_transport.Insecure, TLS: &environment_runtime_transport.Config{}},
	}
	for name, security := range cases {
		t.Run(name, func(t *testing.T) {
			if err := security.Validate(); err == nil {
				t.Fatalf("Validate accepted contradictory or incomplete config: %+v", security)
			}
		})
	}
}

func TestSecurityValidateAcceptsExplicitModes(t *testing.T) {
	for name, security := range map[string]environment_runtime_transport.Security{
		"insecure": {Mode: environment_runtime_transport.Insecure},
		"mtls": {Mode: environment_runtime_transport.MutualTLS, TLS: &environment_runtime_transport.Config{
			CertFile: "/etc/runner/tls.crt",
			KeyFile:  "/etc/runner/tls.key",
			CAFile:   "/etc/runner/ca.pem",
		}},
	} {
		t.Run(name, func(t *testing.T) {
			if err := security.Validate(); err != nil {
				t.Fatalf("Validate rejected valid config: %v", err)
			}
		})
	}
}

func TestSecurityMTLSMisconfigurationDoesNotFallBackToInsecure(t *testing.T) {
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	security := environment_runtime_transport.Security{Mode: environment_runtime_transport.MutualTLS, TLS: &environment_runtime_transport.Config{
		CertFile: "/tmp/runner-transport-test/missing.crt",
		KeyFile:  "/tmp/runner-transport-test/missing.key",
		CAFile:   "/tmp/runner-transport-test/missing-ca.pem",
	}}
	if _, err := security.ClientCredentials(runnerURI); err == nil {
		t.Fatal("ClientCredentials accepted missing mTLS files")
	}
	if _, err := security.ServerCredentials(environment_runtime_transport.GatewayURI); err == nil {
		t.Fatal("ServerCredentials accepted missing mTLS files")
	}
}

func TestSecurityInsecureUsesPlaintextCredentialsAndSkipsPeerAuthentication(t *testing.T) {
	security := environment_runtime_transport.Security{Mode: environment_runtime_transport.Insecure}
	client, err := security.ClientCredentials("unused in insecure mode")
	if err != nil {
		t.Fatal(err)
	}
	server, err := security.ServerCredentials("unused in insecure mode")
	if err != nil {
		t.Fatal(err)
	}
	clientInfo, serverInfo, err := handshake(t, client, server)
	if err != nil {
		t.Fatalf("plaintext credentials handshake failed: %v", err)
	}
	if clientInfo == nil || serverInfo == nil || clientInfo.AuthType() != "insecure" || serverInfo.AuthType() != "insecure" {
		t.Fatalf("plaintext handshake returned unexpected auth info: client %v, server %v", clientInfo, serverInfo)
	}
	if err := security.AuthorizePeer(context.Background(), "unused in insecure mode"); err != nil {
		t.Fatalf("insecure mode required peer authentication: %v", err)
	}
}

func TestSecurityMTLSAuthorizesExactVerifiedIdentity(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	otherEnvironmentRuntimeURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "other-pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	runnerTLS := ca.Issue(t, runnerURI)
	gatewayTLS := ca.Issue(t, environment_runtime_transport.GatewayURI)
	runnerSecurity := environment_runtime_transport.Security{Mode: environment_runtime_transport.MutualTLS, TLS: &runnerTLS}
	gatewaySecurity := environment_runtime_transport.Security{Mode: environment_runtime_transport.MutualTLS, TLS: &gatewayTLS}
	clientCredentials, err := gatewaySecurity.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runnerSecurity.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	clientInfo, serverInfo, err := handshake(t, clientCredentials, serverCredentials)
	if err != nil {
		t.Fatal(err)
	}
	clientContext := peer.NewContext(context.Background(), &peer.Peer{AuthInfo: clientInfo})
	serverContext := peer.NewContext(context.Background(), &peer.Peer{AuthInfo: serverInfo})
	if err := gatewaySecurity.AuthorizePeer(clientContext, runnerURI); err != nil {
		t.Fatalf("client rejected verified Runner identity: %v", err)
	}
	if err := runnerSecurity.AuthorizePeer(serverContext, environment_runtime_transport.GatewayURI); err != nil {
		t.Fatalf("server rejected verified Gateway identity: %v", err)
	}
	if err := gatewaySecurity.AuthorizePeer(clientContext, otherEnvironmentRuntimeURI); err == nil {
		t.Fatal("client authorized the wrong verified Runner identity")
	}
	if err := gatewaySecurity.AuthorizePeer(context.Background(), runnerURI); err == nil {
		t.Fatal("mTLS mode authorized a peer without verified TLS identity")
	}
}
