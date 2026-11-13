package peertls_test

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/credentials/workloadid"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"google.golang.org/grpc/peer"
)

func TestSecurityValidateRequiresKnownSerializedMode(t *testing.T) {
	for name, input := range map[string]string{
		"missing": `{}`,
		"unknown": `{"mode":"plaintext"}`,
	} {
		t.Run(name, func(t *testing.T) {
			var security peertls.Security
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
	cases := map[string]peertls.Security{
		"mtls without TLS": {Mode: peertls.MutualTLS},
		"missing certificate": {Mode: peertls.MutualTLS, TLS: &peertls.Config{
			KeyFile: "/etc/runner/tls.key",
			CAFile:  "/etc/runner/ca.pem",
		}},
		"relative CA path": {Mode: peertls.MutualTLS, TLS: &peertls.Config{
			CertFile: "/etc/runner/tls.crt",
			KeyFile:  "/etc/runner/tls.key",
			CAFile:   "ca.pem",
		}},
		"relative client CA path": {Mode: peertls.MutualTLS, TLS: &peertls.Config{
			CertFile:     "/etc/runner/tls.crt",
			KeyFile:      "/etc/runner/tls.key",
			CAFile:       "/etc/runner/ca.pem",
			ClientCAFile: "client-ca.pem",
		}},
		"client CA with Workload API": {Mode: peertls.MutualTLS, TLS: &peertls.Config{
			WorkloadAPISocket: "unix:///run/agentplane/workload.sock",
			ClientCAFile:      "/etc/runner/client-ca.pem",
		}},
		"insecure with TLS": {Mode: peertls.Insecure, TLS: &peertls.Config{}},
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
	for name, security := range map[string]peertls.Security{
		"insecure": {Mode: peertls.Insecure},
		"mtls": {Mode: peertls.MutualTLS, TLS: &peertls.Config{
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
	runnerURI, err := workloadid.ID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "pod-uid"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	security := peertls.Security{Mode: peertls.MutualTLS, TLS: &peertls.Config{
		CertFile: "/tmp/runner-transport-test/missing.crt",
		KeyFile:  "/tmp/runner-transport-test/missing.key",
		CAFile:   "/tmp/runner-transport-test/missing-ca.pem",
	}}
	if _, err := security.ClientCredentials(runnerURI); err == nil {
		t.Fatal("ClientCredentials accepted missing mTLS files")
	}
	if _, err := security.ServerCredentials(peertls.GatewayURI); err == nil {
		t.Fatal("ServerCredentials accepted missing mTLS files")
	}
}

func TestSecurityInsecureUsesPlaintextCredentialsAndSkipsPeerAuthentication(t *testing.T) {
	security := peertls.Security{Mode: peertls.Insecure}
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
	runnerURI, err := workloadid.ID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "pod-uid"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	otherEnvironmentRuntimeURI, err := workloadid.ID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "other-pod-uid"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	runnerTLS := ca.Issue(t, runnerURI)
	gatewayTLS := ca.Issue(t, peertls.GatewayURI)
	runnerSecurity := peertls.Security{Mode: peertls.MutualTLS, TLS: &runnerTLS}
	gatewaySecurity := peertls.Security{Mode: peertls.MutualTLS, TLS: &gatewayTLS}
	clientCredentials, err := gatewaySecurity.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runnerSecurity.ServerCredentials(peertls.GatewayURI)
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
	if err := runnerSecurity.AuthorizePeer(serverContext, peertls.GatewayURI); err != nil {
		t.Fatalf("server rejected verified Gateway identity: %v", err)
	}
	if err := gatewaySecurity.AuthorizePeer(clientContext, otherEnvironmentRuntimeURI); err == nil {
		t.Fatal("client authorized the wrong verified Runner identity")
	}
	if err := gatewaySecurity.AuthorizePeer(context.Background(), runnerURI); err == nil {
		t.Fatal("mTLS mode authorized a peer without verified TLS identity")
	}
}
