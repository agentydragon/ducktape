package runner_transport_test

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	runnertransporttesting "github.com/agentydragon/ducktape/agentplane/runner_transport/testing"
	"google.golang.org/grpc/peer"
)

func TestSecurityValidateRequiresKnownSerializedMode(t *testing.T) {
	for name, input := range map[string]string{
		"missing": `{}`,
		"unknown": `{"mode":"plaintext"}`,
	} {
		t.Run(name, func(t *testing.T) {
			var security runner_transport.Security
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
	cases := map[string]runner_transport.Security{
		"mtls without TLS": {Mode: runner_transport.MutualTLS},
		"missing certificate": {Mode: runner_transport.MutualTLS, TLS: &runner_transport.Config{
			KeyFile: "/etc/runner/tls.key",
			CAFile:  "/etc/runner/ca.pem",
		}},
		"relative CA path": {Mode: runner_transport.MutualTLS, TLS: &runner_transport.Config{
			CertFile: "/etc/runner/tls.crt",
			KeyFile:  "/etc/runner/tls.key",
			CAFile:   "ca.pem",
		}},
		"insecure with TLS": {Mode: runner_transport.Insecure, TLS: &runner_transport.Config{}},
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
	for name, security := range map[string]runner_transport.Security{
		"insecure": {Mode: runner_transport.Insecure},
		"mtls": {Mode: runner_transport.MutualTLS, TLS: &runner_transport.Config{
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
	runnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	security := runner_transport.Security{Mode: runner_transport.MutualTLS, TLS: &runner_transport.Config{
		CertFile: "/tmp/runner-transport-test/missing.crt",
		KeyFile:  "/tmp/runner-transport-test/missing.key",
		CAFile:   "/tmp/runner-transport-test/missing-ca.pem",
	}}
	if _, err := security.ClientCredentials(runnerURI); err == nil {
		t.Fatal("ClientCredentials accepted missing mTLS files")
	}
	if _, err := security.ServerCredentials(runner_transport.GatewayURI); err == nil {
		t.Fatal("ServerCredentials accepted missing mTLS files")
	}
}

func TestSecurityInsecureUsesPlaintextCredentialsAndSkipsPeerAuthentication(t *testing.T) {
	security := runner_transport.Security{Mode: runner_transport.Insecure}
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
	ca := runnertransporttesting.NewCA(t)
	runnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	otherRunnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "other-pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	runnerTLS := ca.Issue(t, runnerURI)
	gatewayTLS := ca.Issue(t, runner_transport.GatewayURI)
	runnerSecurity := runner_transport.Security{Mode: runner_transport.MutualTLS, TLS: &runnerTLS}
	gatewaySecurity := runner_transport.Security{Mode: runner_transport.MutualTLS, TLS: &gatewayTLS}
	clientCredentials, err := gatewaySecurity.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runnerSecurity.ServerCredentials(runner_transport.GatewayURI)
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
	if err := runnerSecurity.AuthorizePeer(serverContext, runner_transport.GatewayURI); err != nil {
		t.Fatalf("server rejected verified Gateway identity: %v", err)
	}
	if err := gatewaySecurity.AuthorizePeer(clientContext, otherRunnerURI); err == nil {
		t.Fatal("client authorized the wrong verified Runner identity")
	}
	if err := gatewaySecurity.AuthorizePeer(context.Background(), runnerURI); err == nil {
		t.Fatal("mTLS mode authorized a peer without verified TLS identity")
	}
}
