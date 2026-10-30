package runner_transport_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	runnertransporttesting "github.com/agentydragon/ducktape/agentplane/runner_transport/testing"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
)

func TestGatewayClientAuthenticatesRunnerServer(t *testing.T) {
	ca := runnertransporttesting.NewCA(t)
	runnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	runner := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageServerAuth)
	gateway := ca.IssueWithUsage(t, runner_transport.GatewayURI, x509.ExtKeyUsageClientAuth)

	clientCredentials, err := gateway.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	clientInfo, serverInfo, err := handshake(t, clientCredentials, serverCredentials)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := runner_transport.PeerIdentity(peer.NewContext(context.Background(), &peer.Peer{AuthInfo: clientInfo})); err != nil || got != runnerURI {
		t.Fatalf("client saw peer identity %q, error %v", got, err)
	}
	if got, err := runner_transport.PeerIdentity(peer.NewContext(context.Background(), &peer.Peer{AuthInfo: serverInfo})); err != nil || got != runner_transport.GatewayURI {
		t.Fatalf("server saw peer identity %q, error %v", got, err)
	}
	if got, err := runner.LocalIdentity(x509.ExtKeyUsageServerAuth); err != nil || got != runnerURI {
		t.Fatalf("local identity is %q, error %v", got, err)
	}
	if got, err := gateway.LocalIdentity(x509.ExtKeyUsageClientAuth); err != nil || got != runner_transport.GatewayURI {
		t.Fatalf("Gateway local identity is %q, error %v", got, err)
	}
}

func TestClientPinsExactPeerURI(t *testing.T) {
	ca := runnertransporttesting.NewCA(t)
	runnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	otherRunnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "other-pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	runner := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageServerAuth)
	gateway := ca.IssueWithUsage(t, runner_transport.GatewayURI, x509.ExtKeyUsageClientAuth)
	clientCredentials, err := gateway.ClientCredentials(otherRunnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = handshake(t, clientCredentials, serverCredentials)
	if err == nil {
		t.Fatal("handshake succeeded with the wrong expected peer URI")
	}
}

func TestRunnerServerRejectsAnotherRunnerAsClient(t *testing.T) {
	ca := runnertransporttesting.NewCA(t)
	runnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	runner := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageServerAuth)
	client := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageClientAuth)
	clientCredentials, err := client.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := handshake(t, clientCredentials, serverCredentials); err == nil {
		t.Fatal("runner accepted another runner as its Gateway")
	}
}

func TestCredentialsRejectUntrustedRootsAndMissingEKU(t *testing.T) {
	trustedCA := runnertransporttesting.NewCA(t)
	otherCA := runnertransporttesting.NewCA(t)
	runnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	runner := trustedCA.Issue(t, runnerURI)
	gateway := otherCA.Issue(t, runner_transport.GatewayURI)
	clientCredentials, err := gateway.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = handshake(t, clientCredentials, serverCredentials)
	if err == nil {
		t.Fatal("handshake succeeded across untrusted roots")
	}

	wrongUsage := trustedCA.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageServerAuth)
	if _, err := wrongUsage.ClientCredentials(runner_transport.GatewayURI); err == nil {
		t.Fatal("client credentials accepted a certificate without clientAuth EKU")
	}
}

func TestCredentialsRejectExpiredLeafAndRootCertificates(t *testing.T) {
	now := time.Now().UTC()
	validCA := runnertransporttesting.NewCA(t)
	runnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	expiredLeaf := validCA.IssueWithValidity(
		t,
		runnerURI,
		now.Add(-2*time.Hour),
		now.Add(-time.Hour),
		x509.ExtKeyUsageClientAuth,
		x509.ExtKeyUsageServerAuth,
	)
	if _, err := expiredLeaf.ServerCredentials(runner_transport.GatewayURI); err == nil {
		t.Fatal("server credentials accepted an expired leaf certificate")
	}

	expiredCA := runnertransporttesting.NewCAWithValidity(t, now.Add(-2*time.Hour), now.Add(-time.Hour))
	runner := expiredCA.Issue(t, runnerURI)
	if _, err := runner.ServerCredentials(runner_transport.GatewayURI); err == nil {
		t.Fatal("server credentials accepted an expired CA certificate")
	}
}

func TestCredentialsReloadRotatedCertificateAndRoots(t *testing.T) {
	oldCA := runnertransporttesting.NewCA(t)
	newCA := runnertransporttesting.NewCA(t)
	runnerURI, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	runner := oldCA.Issue(t, runnerURI)
	gateway := oldCA.Issue(t, runner_transport.GatewayURI)
	clientCredentials, err := gateway.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(runner_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	beforeInfo, _, err := handshake(t, clientCredentials, serverCredentials)
	if err != nil {
		t.Fatal(err)
	}
	beforeLeaf := peerLeaf(t, beforeInfo)

	rotatedRunner := newCA.Issue(t, runnerURI)
	oldRoots, err := os.ReadFile(oldCA.RootFile)
	if err != nil {
		t.Fatal(err)
	}
	newRoots, err := os.ReadFile(newCA.RootFile)
	if err != nil {
		t.Fatal(err)
	}
	replaceFile(t, oldCA.RootFile, append(oldRoots, newRoots...))
	copyFile(t, rotatedRunner.CertFile, runner.CertFile)
	copyFile(t, rotatedRunner.KeyFile, runner.KeyFile)

	afterInfo, _, err := handshake(t, clientCredentials, serverCredentials)
	if err != nil {
		t.Fatal(err)
	}
	afterLeaf := peerLeaf(t, afterInfo)
	if string(beforeLeaf.Raw) == string(afterLeaf.Raw) {
		t.Fatal("second handshake used the old server certificate")
	}
	if got, err := runner.LocalIdentity(x509.ExtKeyUsageServerAuth); err != nil || got != runnerURI {
		t.Fatalf("rotated local identity is %q, error %v", got, err)
	}
}

func TestPeerIdentityRequiresTLSPeerCertificate(t *testing.T) {
	cases := []struct {
		name string
		info credentials.AuthInfo
	}{
		{name: "no auth info"},
		{name: "TLS without peer certificate", info: credentials.TLSInfo{}},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			ctx := peer.NewContext(context.Background(), &peer.Peer{AuthInfo: testCase.info})
			if _, err := runner_transport.PeerIdentity(ctx); err == nil {
				t.Fatal("PeerIdentity accepted unauthenticated peer metadata")
			}
		})
	}
}

func TestPeerIdentityRejectsExpiredLeafOnExistingConnection(t *testing.T) {
	identity, err := url.Parse("spiffe://agentplane/runner/agent-ns/sandbox-uid/credential-id")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	expiredLeaf := &x509.Certificate{
		URIs:      []*url.URL{identity},
		NotBefore: now.Add(-2 * time.Hour),
		NotAfter:  now.Add(-time.Hour),
	}
	ctx := peer.NewContext(context.Background(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{
		PeerCertificates: []*x509.Certificate{expiredLeaf},
	}}})
	if _, err := runner_transport.PeerIdentity(ctx); err == nil {
		t.Fatal("PeerIdentity accepted an expired leaf from an existing TLS connection")
	}
}

func TestParseRunnerURIRejectsNonCanonicalAndMalformedPaths(t *testing.T) {
	valid, err := runner_transport.RunnerURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	namespace, sandboxUID, workloadUID, err := runner_transport.ParseRunnerURI(valid)
	if err != nil {
		t.Fatal(err)
	}
	if namespace != "agent-ns" || sandboxUID != "sandbox-uid" || workloadUID != "pod-uid" {
		t.Fatalf("parsed identity = (%q, %q, %q)", namespace, sandboxUID, workloadUID)
	}
	for _, invalid := range []string{
		"spiffe://agentplane/runner/agent-ns/sandbox-uid",
		"spiffe://agentplane/runner/agent-ns/sandbox-uid/pod-uid/extra",
		"spiffe://agentplane/runner/Agent-ns/sandbox-uid/pod-uid",
		"spiffe://agentplane/runner/agent-ns/sandbox%2Fuid/pod-uid",
		"spiffe://other/runner/agent-ns/sandbox-uid/pod-uid",
	} {
		if _, _, _, err := runner_transport.ParseRunnerURI(invalid); err == nil {
			t.Errorf("ParseRunnerURI accepted %q", invalid)
		}
	}
}

func handshake(t *testing.T, client, server credentials.TransportCredentials) (clientInfo, serverInfo credentials.AuthInfo, err error) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	clientRaw, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	serverRaw, err := listener.Accept()
	if err != nil {
		clientRaw.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = clientRaw.Close()
		_ = serverRaw.Close()
	})
	type result struct {
		conn net.Conn
		info credentials.AuthInfo
		err  error
	}
	serverDone := make(chan result, 1)
	go func() {
		conn, info, err := server.ServerHandshake(serverRaw)
		serverDone <- result{conn: conn, info: info, err: err}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	clientConn, clientInfo, clientErr := client.ClientHandshake(ctx, "gateway.test:443", clientRaw)
	if clientErr != nil {
		_ = clientRaw.Close()
		_ = serverRaw.Close()
	}
	serverResult := <-serverDone
	if clientConn != nil {
		_ = clientConn.Close()
	}
	if serverResult.conn != nil {
		_ = serverResult.conn.Close()
	}
	return clientInfo, serverResult.info, errors.Join(clientErr, serverResult.err)
}

func peerLeaf(t *testing.T, info credentials.AuthInfo) *x509.Certificate {
	t.Helper()
	tlsInfo, ok := info.(credentials.TLSInfo)
	if !ok || len(tlsInfo.State.PeerCertificates) == 0 {
		t.Fatalf("handshake returned incomplete TLS peer info: %T", info)
	}
	return tlsInfo.State.PeerCertificates[0]
}

func replaceFile(t *testing.T, path string, contents []byte) {
	t.Helper()
	temporary := filepath.Join(filepath.Dir(path), "rotated-"+filepath.Base(path))
	if err := os.WriteFile(temporary, contents, 0o600); err != nil {
		t.Fatalf("write replacement file: %v", err)
	}
	if err := os.Rename(temporary, path); err != nil {
		t.Fatalf("replace file: %v", err)
	}
}

func copyFile(t *testing.T, source, destination string) {
	t.Helper()
	contents, err := os.ReadFile(source)
	if err != nil {
		t.Fatal(err)
	}
	replaceFile(t, destination, contents)
}
