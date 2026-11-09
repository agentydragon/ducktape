package environment_runtime_transport_test

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/peer"
)

func TestGatewayClientAuthenticatesEnvironmentRuntimeServer(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "pod-uid"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	runner := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageServerAuth)
	gateway := ca.IssueWithUsage(t, environment_runtime_transport.GatewayURI, x509.ExtKeyUsageClientAuth)

	clientCredentials, err := gateway.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	clientInfo, serverInfo, err := handshake(t, clientCredentials, serverCredentials)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := environment_runtime_transport.PeerIdentity(peer.NewContext(context.Background(), &peer.Peer{AuthInfo: clientInfo})); err != nil || got != runnerURI {
		t.Fatalf("client saw peer identity %q, error %v", got, err)
	}
	if got, err := environment_runtime_transport.PeerIdentity(peer.NewContext(context.Background(), &peer.Peer{AuthInfo: serverInfo})); err != nil || got != environment_runtime_transport.GatewayURI {
		t.Fatalf("server saw peer identity %q, error %v", got, err)
	}
	if got, err := runner.LocalIdentity(x509.ExtKeyUsageServerAuth); err != nil || got != runnerURI {
		t.Fatalf("local identity is %q, error %v", got, err)
	}
	if got, err := gateway.LocalIdentity(x509.ExtKeyUsageClientAuth); err != nil || got != environment_runtime_transport.GatewayURI {
		t.Fatalf("Gateway local identity is %q, error %v", got, err)
	}
}

func TestWorkloadTLSConfigPinsEgressAndRequiresRuntimeClientAuth(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runtimeURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	serverConfig := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	clientConfig := ca.IssueWithUsage(t, runtimeURI, x509.ExtKeyUsageClientAuth)
	serverTLS, err := serverConfig.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS, err := clientConfig.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	clientState, serverState, err := tlsHandshake(t, clientTLS, serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	if got, err := environment_runtime_transport.PeerIdentityFromTLSState(clientState, x509.ExtKeyUsageServerAuth); err != nil || got != environment_runtime_transport.EgressProxyURI {
		t.Fatalf("server identity = %q, error %v", got, err)
	}
	if got, err := environment_runtime_transport.PeerIdentityFromTLSState(serverState, x509.ExtKeyUsageClientAuth); err != nil || got != runtimeURI {
		t.Fatalf("client identity = %q, error %v", got, err)
	}
	clientCredentials, err := clientConfig.ClientCredentials(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := serverConfig.WorkloadServerCredentials(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	clientInfo, serverInfo, err := handshake(t, clientCredentials, serverCredentials)
	if err != nil {
		t.Fatalf("workload server gRPC credentials handshake: %v", err)
	}
	if got, err := environment_runtime_transport.PeerIdentity(peer.NewContext(context.Background(), &peer.Peer{AuthInfo: clientInfo})); err != nil || got != environment_runtime_transport.EgressProxyURI {
		t.Fatalf("gRPC client saw proxy identity %q, error %v", got, err)
	}
	if got, err := environment_runtime_transport.PeerIdentity(peer.NewContext(context.Background(), &peer.Peer{AuthInfo: serverInfo})); err != nil || got != runtimeURI {
		t.Fatalf("gRPC proxy saw runtime identity %q, error %v", got, err)
	}
	if _, err := environment_runtime_transport.PeerIdentityFromTLSState(serverState, x509.ExtKeyUsageServerAuth); err == nil {
		t.Fatal("peer helper accepted a certificate without the requested EKU")
	}
	if _, err := clientConfig.ClientTLSConfig(environment_runtime_transport.GatewayURI); err == nil {
		t.Fatal("client config accepted a non-egress peer URI")
	}
	if _, err := serverConfig.WorkloadServerTLSConfig(environment_runtime_transport.GatewayURI); err == nil {
		t.Fatal("server config accepted a non-egress local URI")
	}

	wrongEKU := ca.IssueWithUsage(t, runtimeURI, x509.ExtKeyUsageServerAuth)
	if _, err := wrongEKU.ClientTLSConfig(environment_runtime_transport.EgressProxyURI); err == nil {
		t.Fatal("client config accepted local certificate without clientAuth")
	}
	wrongIdentity := ca.IssueWithUsage(t, environment_runtime_transport.GatewayURI, x509.ExtKeyUsageClientAuth)
	wrongClientTLS, err := wrongIdentity.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := tlsHandshake(t, wrongClientTLS, serverTLS); err == nil {
		t.Fatal("egress proxy accepted a non-runtime client identity")
	}
}

func TestOptionalCertificateTLSStillVerifiesEveryPresentedLeaf(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	uri, err := environment_runtime_transport.WorkloadID{Namespace: "test-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	server := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	client := ca.IssueWithUsage(t, uri, x509.ExtKeyUsageClientAuth)
	clientTLS, err := client.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	withoutCertificate := clientTLS.Clone()
	withoutCertificate.GetClientCertificate = nil
	withoutCertificate.Certificates = nil
	optional, err := server.WorkloadServerTLSConfigWithBearer(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	for name, config := range map[string]*tls.Config{"certificate": clientTLS, "bearer transport": withoutCertificate} {
		t.Run(name, func(t *testing.T) {
			if _, _, err := tlsHandshake(t, config, optional); err != nil {
				t.Fatal(err)
			}
		})
	}
	otherCA := environmentruntimetransporttesting.NewCA(t)
	for name, bad := range map[string]environment_runtime_transport.Config{
		"forged":         otherCA.IssueWithUsage(t, uri, x509.ExtKeyUsageClientAuth),
		"wrong EKU":      ca.IssueWithUsage(t, uri, x509.ExtKeyUsageServerAuth),
		"wrong identity": ca.IssueWithUsage(t, environment_runtime_transport.GatewayURI, x509.ExtKeyUsageClientAuth),
		"expired":        ca.IssueWithValidity(t, uri, time.Now().Add(-2*time.Hour), time.Now().Add(-time.Hour), x509.ExtKeyUsageClientAuth),
	} {
		t.Run(name, func(t *testing.T) {
			pair, err := tls.LoadX509KeyPair(bad.CertFile, bad.KeyFile)
			if err != nil {
				t.Fatal(err)
			}
			config := withoutCertificate.Clone()
			config.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) { return &pair, nil }
			if _, _, err := tlsHandshake(t, config, optional); err == nil {
				t.Fatal("invalid presented certificate accepted")
			}
		})
	}
	mandatory, err := server.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := tlsHandshake(t, withoutCertificate, mandatory); err == nil {
		t.Fatal("SPIFFE-only listener accepted no certificate")
	}
	bearerOnly, err := (environment_runtime_transport.Config{CertFile: server.CertFile, KeyFile: server.KeyFile}).BearerServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := tlsHandshake(t, withoutCertificate, bearerOnly); err != nil {
		t.Fatal(err)
	}
	if _, _, err := tlsHandshake(t, clientTLS, bearerOnly); err == nil {
		t.Fatal("bearer-only listener accepted certificate authentication")
	}
}

func TestClientTLSConfigReloadsSameIdentityAndRejectsChangedIdentity(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runtimeURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	otherRuntimeURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "rotated-credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	proxy := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	client := ca.IssueWithUsage(t, runtimeURI, x509.ExtKeyUsageClientAuth)
	proxyTLS, err := proxy.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS, err := client.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}

	_, beforeServerState, err := tlsHandshake(t, clientTLS, proxyTLS)
	if err != nil {
		t.Fatalf("initial handshake: %v", err)
	}
	if len(beforeServerState.PeerCertificates) == 0 {
		t.Fatal("initial handshake returned no client certificate to the proxy")
	}
	before := beforeServerState.PeerCertificates[0]
	if got, err := environment_runtime_transport.PeerIdentityFromTLSState(beforeServerState, x509.ExtKeyUsageClientAuth); err != nil || got != runtimeURI {
		t.Fatalf("initial client identity = %q, error %v", got, err)
	}

	rotatedClient := ca.IssueWithUsage(t, runtimeURI, x509.ExtKeyUsageClientAuth)
	copyFile(t, rotatedClient.CertFile, client.CertFile)
	copyFile(t, rotatedClient.KeyFile, client.KeyFile)
	afterClientState, afterServerState, err := tlsHandshake(t, clientTLS, proxyTLS)
	if err != nil {
		t.Fatalf("same-identity rotated handshake: %v", err)
	}
	if len(afterClientState.PeerCertificates) == 0 {
		t.Fatal("rotated handshake returned no proxy identity")
	}
	if got, err := environment_runtime_transport.PeerIdentityFromTLSState(afterClientState, x509.ExtKeyUsageServerAuth); err != nil || got != environment_runtime_transport.EgressProxyURI {
		t.Fatalf("rotated proxy identity = %q, error %v", got, err)
	}
	if len(afterServerState.PeerCertificates) == 0 {
		t.Fatal("rotated handshake returned no client certificate to the proxy")
	}
	after := afterServerState.PeerCertificates[0]
	if string(before.Raw) == string(after.Raw) {
		t.Fatal("rotated handshake reused the old client certificate")
	}
	if got, err := environment_runtime_transport.PeerIdentityFromTLSState(afterServerState, x509.ExtKeyUsageClientAuth); err != nil || got != runtimeURI {
		t.Fatalf("rotated client identity = %q, error %v", got, err)
	}

	changedIdentity := ca.IssueWithUsage(t, otherRuntimeURI, x509.ExtKeyUsageClientAuth)
	copyFile(t, changedIdentity.CertFile, client.CertFile)
	copyFile(t, changedIdentity.KeyFile, client.KeyFile)
	if _, _, err := tlsHandshake(t, clientTLS, proxyTLS); err == nil {
		t.Fatal("rotated handshake accepted a changed local SPIFFE identity")
	}
}

func tlsHandshake(t *testing.T, clientConfig, serverConfig *tls.Config) (tls.ConnectionState, tls.ConnectionState, error) {
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
		_ = clientRaw.Close()
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	_ = clientRaw.SetDeadline(deadline)
	_ = serverRaw.SetDeadline(deadline)
	client := tls.Client(clientRaw, clientConfig)
	server := tls.Server(serverRaw, serverConfig)
	serverResult := make(chan error, 1)
	go func() { serverResult <- server.Handshake() }()
	clientErr := client.Handshake()
	if clientErr != nil {
		_ = clientRaw.Close()
		_ = serverRaw.Close()
	}
	serverErr := <-serverResult
	clientState, serverState := client.ConnectionState(), server.ConnectionState()
	_ = clientRaw.Close()
	_ = serverRaw.Close()
	if clientErr != nil {
		return tls.ConnectionState{}, tls.ConnectionState{}, clientErr
	}
	if serverErr != nil {
		return tls.ConnectionState{}, tls.ConnectionState{}, serverErr
	}
	return clientState, serverState, nil
}

func TestClientPinsExactPeerURI(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "pod-uid"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	otherEnvironmentRuntimeURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "other-pod-uid"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	runner := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageServerAuth)
	gateway := ca.IssueWithUsage(t, environment_runtime_transport.GatewayURI, x509.ExtKeyUsageClientAuth)
	clientCredentials, err := gateway.ClientCredentials(otherEnvironmentRuntimeURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = handshake(t, clientCredentials, serverCredentials)
	if err == nil {
		t.Fatal("handshake succeeded with the wrong expected peer URI")
	}
}

func TestEnvironmentRuntimeServerRejectsAnotherRunnerAsClient(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	runner := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageServerAuth)
	client := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageClientAuth)
	clientCredentials, err := client.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := handshake(t, clientCredentials, serverCredentials); err == nil {
		t.Fatal("runner accepted another runner as its Gateway")
	}
}

// The runner trusts the cluster root for its own chain and the servers it dials, and the workload
// CA alone for the Gateway's client certificate: a leaf the root signs directly naming the Gateway
// is refused, through both the file-backed and the opened sources.
func TestServerVerifiesClientsAgainstClientCAFileAlone(t *testing.T) {
	root := environmentruntimetransporttesting.NewCA(t)
	workload := root.Intermediate(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	runner := workload.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth)
	runner.CAFile = root.RootFile
	gateway := workload.IssueWithUsage(t, environment_runtime_transport.GatewayURI, x509.ExtKeyUsageClientAuth)
	gateway.CAFile = root.RootFile
	impostor := root.IssueWithUsage(t, environment_runtime_transport.GatewayURI, x509.ExtKeyUsageClientAuth)

	clientOf := func(config environment_runtime_transport.Config) credentials.TransportCredentials {
		creds, err := config.ClientCredentials(runnerURI)
		if err != nil {
			t.Fatal(err)
		}
		return creds
	}
	rootOnly := runner
	rootOnly.ClientCAFile = ""
	rootOnlyServer, err := rootOnly.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := handshake(t, clientOf(impostor), rootOnlyServer); err != nil {
		t.Fatalf("without client_ca_file the cluster root still verifies clients: %v", err)
	}

	runner.ClientCAFile = workload.RootFile
	fileServer, err := runner.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	opened, err := (environment_runtime_transport.Security{Mode: environment_runtime_transport.MutualTLS, TLS: &runner}).Open(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer opened.Close()
	openedServer, err := opened.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	for name, server := range map[string]credentials.TransportCredentials{"file": fileServer, "opened": openedServer} {
		t.Run(name, func(t *testing.T) {
			if _, serverInfo, err := handshake(t, clientOf(gateway), server); err != nil {
				t.Fatalf("workload CA Gateway was refused: %v", err)
			} else if got, err := environment_runtime_transport.PeerIdentity(peer.NewContext(context.Background(), &peer.Peer{AuthInfo: serverInfo})); err != nil || got != environment_runtime_transport.GatewayURI {
				t.Fatalf("server saw peer identity %q, error %v", got, err)
			}
			if _, _, err := handshake(t, clientOf(impostor), server); err == nil {
				t.Fatal("a root-signed leaf naming the Gateway authenticated as a client")
			}
		})
	}

	// Dialing, the runner still verifies servers against ca_file: a root-signed server passes.
	service := root.IssueWithUsage(t, environment_runtime_transport.HistoryIngressURI, x509.ExtKeyUsageServerAuth)
	serviceServer, err := service.ServerCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	runnerClient, err := opened.ClientCredentials(environment_runtime_transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := handshake(t, runnerClient, serviceServer); err != nil {
		t.Fatalf("client_ca_file changed the trust the runner dials with: %v", err)
	}
}

func TestCredentialsRejectUntrustedRootsAndMissingEKU(t *testing.T) {
	trustedCA := environmentruntimetransporttesting.NewCA(t)
	otherCA := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "pod-uid"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	runner := trustedCA.Issue(t, runnerURI)
	gateway := otherCA.Issue(t, environment_runtime_transport.GatewayURI)
	clientCredentials, err := gateway.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = handshake(t, clientCredentials, serverCredentials)
	if err == nil {
		t.Fatal("handshake succeeded across untrusted roots")
	}

	wrongUsage := trustedCA.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageServerAuth)
	if _, err := wrongUsage.ClientCredentials(environment_runtime_transport.GatewayURI); err == nil {
		t.Fatal("client credentials accepted a certificate without clientAuth EKU")
	}
}

func TestCredentialsRejectExpiredLeafAndRootCertificates(t *testing.T) {
	now := time.Now().UTC()
	validCA := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
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
	if _, err := expiredLeaf.ServerCredentials(environment_runtime_transport.GatewayURI); err == nil {
		t.Fatal("server credentials accepted an expired leaf certificate")
	}

	expiredCA := environmentruntimetransporttesting.NewCAWithValidity(t, now.Add(-2*time.Hour), now.Add(-time.Hour))
	runner := expiredCA.Issue(t, runnerURI)
	if _, err := runner.ServerCredentials(environment_runtime_transport.GatewayURI); err == nil {
		t.Fatal("server credentials accepted an expired CA certificate")
	}
}

func TestServerCredentialsDisableTLS13SessionResumption(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runtimeURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	local := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	runtime := ca.IssueWithUsage(t, runtimeURI, x509.ExtKeyUsageClientAuth)
	serverTLS, err := local.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	// A stable key ensures the second handshake would resume if the expiry
	// wrapper left server-side session tickets enabled.
	for i := range serverTLS.SessionTicketKey {
		serverTLS.SessionTicketKey[i] = byte(i + 1)
	}
	selections := 0
	getCertificate := serverTLS.GetCertificate
	serverTLS.GetCertificate = func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
		selections++
		return getCertificate(hello)
	}
	clientTLS, err := runtime.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS.ClientSessionCache = tls.NewLRUClientSessionCache(1)
	clientCredentials := credentials.NewTLS(clientTLS)
	serverCredentials, err := environment_runtime_transport.ServerCredentials(serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		clientInfo, _, err := handshakeWithApplicationData(t, clientCredentials, serverCredentials)
		if err != nil {
			t.Fatalf("handshake %d: %v", i+1, err)
		}
		tlsInfo, ok := clientInfo.(credentials.TLSInfo)
		if !ok || tlsInfo.State.DidResume {
			t.Fatalf("client handshake %d resumed or returned %T", i+1, clientInfo)
		}
	}
	if selections != 2 {
		t.Fatalf("server selected a certificate %d times across two handshakes", selections)
	}
}

func TestCredentialsReloadRotatedCertificateAndRoots(t *testing.T) {
	oldCA := environmentruntimetransporttesting.NewCA(t)
	newCA := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "pod-uid"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	runner := oldCA.Issue(t, runnerURI)
	gateway := oldCA.Issue(t, environment_runtime_transport.GatewayURI)
	clientCredentials, err := gateway.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := runner.ServerCredentials(environment_runtime_transport.GatewayURI)
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

func TestGRPCClientConnReconnectsAfterFileSVIDExpiryAndRotation(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	short := ca.IssueWithValidity(t, runnerURI, now.Add(-time.Minute), now.Add(4*time.Second), x509.ExtKeyUsageClientAuth)
	rotated := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageClientAuth)
	gateway := ca.IssueWithUsage(t, environment_runtime_transport.GatewayURI, x509.ExtKeyUsageServerAuth)
	clientCredentials, err := short.ClientCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := gateway.ServerCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	shortLeaf, err := x509.ParseCertificate(readCertificateDER(t, short.CertFile))
	if err != nil {
		t.Fatal(err)
	}
	assertGRPCClientReconnectsAfterExpiry(t, clientCredentials, serverCredentials, shortLeaf.NotAfter, func() {
		copyFile(t, rotated.CertFile, short.CertFile)
		copyFile(t, rotated.KeyFile, short.KeyFile)
	})
}

func TestGRPCClientConnReconnectsAfterWorkloadAPISVIDExpiryAndRotation(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "workload-credential"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	short := ca.IssueWithValidity(t, runnerURI, now.Add(-time.Minute), now.Add(4*time.Second), x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth)
	rotated := ca.IssueWithUsage(t, runnerURI, x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth)
	gateway := ca.IssueWithUsage(t, environment_runtime_transport.GatewayURI, x509.ExtKeyUsageServerAuth)
	rootDER := readCertificateDER(t, ca.RootFile)
	workloadServer := newWorkloadAPITestServer(t, workloadX509SVID(t, short, runnerURI, rootDER), rootDER)
	opened, err := (environment_runtime_transport.Security{
		Mode: environment_runtime_transport.MutualTLS,
		TLS:  &environment_runtime_transport.Config{WorkloadAPISocket: workloadServer.endpoint},
	}).Open(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer opened.Close()
	clientCredentials, err := opened.ClientCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := gateway.ServerCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	shortLeaf, err := x509.ParseCertificate(readCertificateDER(t, short.CertFile))
	if err != nil {
		t.Fatal(err)
	}
	rotatedDER := readCertificateDER(t, rotated.CertFile)
	assertGRPCClientReconnectsAfterExpiry(t, clientCredentials, serverCredentials, shortLeaf.NotAfter, func() {
		workloadServer.updates <- workloadX509SVID(t, rotated, runnerURI, rootDER)
		time.Sleep(100 * time.Millisecond)
		waitForWorkloadClientHandshake(t, clientCredentials, serverCredentials, rotatedDER)
	})
	if err := opened.Close(); err != nil {
		t.Fatal(err)
	}
	workloadServer.waitForStreamsToClose(t)
}

func waitForWorkloadClientHandshake(t *testing.T, client, server credentials.TransportCredentials, want []byte) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, serverInfo, err := handshake(t, client, server)
		if err != nil {
			t.Fatalf("handshake during Workload API rotation: %v", err)
		}
		if got := peerLeaf(t, serverInfo); bytes.Equal(got.Raw, want) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("Workload API source did not rotate the presented client certificate")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func assertGRPCClientReconnectsAfterExpiry(
	t *testing.T,
	clientCredentials, serverCredentials credentials.TransportCredentials,
	oldCertificateNotAfter time.Time,
	rotate func(),
) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	peerCertificates := make(chan []byte, 8)
	server := grpc.NewServer(
		grpc.Creds(serverCredentials),
		grpc.UnaryInterceptor(func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			remote, ok := peer.FromContext(ctx)
			if !ok {
				return nil, errors.New("gRPC peer missing")
			}
			tlsInfo, ok := remote.AuthInfo.(credentials.TLSInfo)
			if !ok || len(tlsInfo.State.PeerCertificates) == 0 {
				return nil, errors.New("gRPC TLS peer missing")
			}
			select {
			case peerCertificates <- append([]byte(nil), tlsInfo.State.PeerCertificates[0].Raw...):
			default:
			}
			return handler(ctx, request)
		}),
	)
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)
	grpc_health_v1.RegisterHealthServer(server, healthServer)
	go func() { _ = server.Serve(listener) }()
	defer server.Stop()

	client, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(clientCredentials))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	invoke := func() error {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		return client.Invoke(ctx, "/grpc.health.v1.Health/Check", &grpc_health_v1.HealthCheckRequest{}, &grpc_health_v1.HealthCheckResponse{})
	}
	if err := invoke(); err != nil {
		t.Fatalf("initial Health.Check: %v", err)
	}
	var initialCertificate []byte
	select {
	case initialCertificate = <-peerCertificates:
	case <-time.After(time.Second):
		t.Fatal("server did not observe initial client certificate")
	}
	streamCtx, cancelStream := context.WithCancel(context.Background())
	defer cancelStream()
	stream, err := grpc_health_v1.NewHealthClient(client).Watch(streamCtx, &grpc_health_v1.HealthCheckRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != nil {
		t.Fatalf("initial Health.Watch: %v", err)
	}
	streamDone := make(chan error, 1)
	go func() { _, err := stream.Recv(); streamDone <- err }()
	rotate()
	select {
	case err := <-streamDone:
		if err == nil {
			t.Fatal("Health.Watch unexpectedly received another response")
		}
	case <-time.After(max(time.Until(oldCertificateNotAfter), 0) + 3*time.Second):
		t.Fatal("active Health.Watch remained open after certificate expiry")
	}
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		if err := invoke(); err == nil {
			select {
			case current := <-peerCertificates:
				if !bytes.Equal(initialCertificate, current) {
					return
				}
			case <-time.After(250 * time.Millisecond):
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("same gRPC ClientConn did not reconnect with the rotated certificate after expiry")
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
			if _, err := environment_runtime_transport.PeerIdentity(ctx); err == nil {
				t.Fatal("PeerIdentity accepted unauthenticated peer metadata")
			}
		})
	}
}

func TestPeerIdentityRejectsExpiredLeafOnExistingConnection(t *testing.T) {
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	identity, err := url.Parse(runnerURI)
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
	if _, err := environment_runtime_transport.PeerIdentity(ctx); err == nil {
		t.Fatal("PeerIdentity accepted an expired leaf from an existing TLS connection")
	}
}

func TestParseWorkloadURIAcceptsOnlyTheCanonicalForm(t *testing.T) {
	workload := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "test-agent.box", CredentialID: "0123456789abcdef0123456789abcdef"}
	valid, err := workload.URI()
	if err != nil {
		t.Fatal(err)
	}
	if valid != "spiffe://agentplane/agent-ns/test-agent.box/0123456789abcdef0123456789abcdef" {
		t.Fatalf("URI = %q", valid)
	}
	if parsed, err := environment_runtime_transport.ParseWorkloadURI(valid); err != nil || parsed != workload {
		t.Fatalf("parsed %+v, %v", parsed, err)
	}
	for _, invalid := range []string{
		"spiffe://agentplane/agent-ns/test-agent",
		"spiffe://agentplane/agent-ns/test-agent/0123456789abcdef0123456789abcdef/extra",
		"spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/0123456789abcdef0123456789abcdef",
		"spiffe://agentplane/Agent-ns/test-agent/0123456789abcdef0123456789abcdef",
		"spiffe://agentplane/agent-ns/Test-agent/0123456789abcdef0123456789abcdef",
		"spiffe://agentplane/agent-ns/test-agent/0123456789ABCDEF0123456789abcdef",
		"spiffe://agentplane/agent-ns/test-agent/credential.id",
		"spiffe://agentplane/agent-ns/-test-agent/0123456789abcdef0123456789abcdef",
		"spiffe://other/agent-ns/test-agent/0123456789abcdef0123456789abcdef",
		"spiffe://agentplane/actions",
	} {
		if _, err := environment_runtime_transport.ParseWorkloadURI(invalid); err == nil {
			t.Errorf("ParseWorkloadURI accepted %q", invalid)
		}
	}
}

func handshake(t *testing.T, client, server credentials.TransportCredentials) (clientInfo, serverInfo credentials.AuthInfo, err error) {
	return handshakeWithApplicationDataMode(t, client, server, false)
}

func handshakeWithApplicationData(t *testing.T, client, server credentials.TransportCredentials) (clientInfo, serverInfo credentials.AuthInfo, err error) {
	return handshakeWithApplicationDataMode(t, client, server, true)
}

func handshakeWithApplicationDataMode(t *testing.T, client, server credentials.TransportCredentials, exchange bool) (clientInfo, serverInfo credentials.AuthInfo, err error) {
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
	if exchange && clientErr == nil && serverResult.err == nil {
		if _, err := serverResult.conn.Write([]byte("ticket")); err != nil {
			clientErr = fmt.Errorf("write TLS ticket processing data: %w", err)
		} else {
			_ = clientConn.SetReadDeadline(time.Now().Add(3 * time.Second))
			if _, err := io.ReadFull(clientConn, make([]byte, len("ticket"))); err != nil {
				clientErr = fmt.Errorf("read TLS ticket processing data: %w", err)
			}
		}
	}
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

func readCertificateDER(t *testing.T, path string) []byte {
	t.Helper()
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode(contents)
	if block == nil {
		t.Fatalf("%s does not contain a PEM certificate", path)
	}
	if _, err := x509.ParseCertificate(block.Bytes); err != nil {
		t.Fatal(err)
	}
	return block.Bytes
}

// Each endpoint must enforce both expiries even when the other endpoint uses
// ordinary gRPC TLS credentials, as during a rolling update.
func TestCertificateExpiryClosesEachEndpointIndependently(t *testing.T) {
	for _, serverEnforces := range []bool{false, true} {
		for _, serverExpires := range []bool{false, true} {
			name := fmt.Sprintf("server_enforces=%t/server_expires=%t", serverEnforces, serverExpires)
			t.Run(name, func(t *testing.T) {
				t.Parallel()
				ca := environmentruntimetransporttesting.NewCA(t)
				clientURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
				if err != nil {
					t.Fatal(err)
				}
				now := time.Now()
				clientEnd, serverEnd := now.Add(time.Hour), now.Add(time.Hour)
				if serverExpires {
					serverEnd = now.Add(5 * time.Second)
				} else {
					clientEnd = now.Add(5 * time.Second)
				}
				clientConfig := ca.IssueWithValidity(t, clientURI, now.Add(-time.Minute), clientEnd, x509.ExtKeyUsageClientAuth)
				serverConfig := ca.IssueWithValidity(t, environment_runtime_transport.GatewayURI, now.Add(-time.Minute), serverEnd, x509.ExtKeyUsageServerAuth)
				var clientCreds, serverCreds credentials.TransportCredentials
				if serverEnforces {
					clientCreds = standardTestCredentials(t, clientConfig, false)
					serverCreds, err = serverConfig.ServerCredentials(clientURI)
				} else {
					clientCreds, err = clientConfig.ClientCredentials(environment_runtime_transport.GatewayURI)
					serverCreds = standardTestCredentials(t, serverConfig, true)
				}
				if err != nil {
					t.Fatal(err)
				}
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
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = clientRaw.Close(); _ = serverRaw.Close() })
				type result struct {
					conn net.Conn
					err  error
				}
				serverResult := make(chan result, 1)
				go func() {
					conn, _, err := serverCreds.ServerHandshake(serverRaw)
					serverResult <- result{conn, err}
				}()
				ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
				defer cancel()
				client, _, err := clientCreds.ClientHandshake(ctx, "example.test:443", clientRaw)
				if err != nil {
					t.Fatal(err)
				}
				defer client.Close()
				server := <-serverResult
				if server.err != nil {
					t.Fatal(server.err)
				}
				defer server.conn.Close()
				wrapped := client
				if serverEnforces {
					wrapped = server.conn
				}
				_ = wrapped.SetReadDeadline(now.Add(8 * time.Second))
				var data [1]byte
				_, err = wrapped.Read(data[:])
				if err == nil {
					t.Fatal("connection remained readable after expiry")
				}
				if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
					t.Fatalf("connection did not close on expiry: %v", err)
				}
			})
		}
	}
}

func standardTestCredentials(t *testing.T, config environment_runtime_transport.Config, server bool) credentials.TransportCredentials {
	t.Helper()
	certificate, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	rootsPEM, err := os.ReadFile(config.CAFile)
	if err != nil {
		t.Fatal(err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(rootsPEM) {
		t.Fatal("invalid test root")
	}
	conf := &tls.Config{Certificates: []tls.Certificate{certificate}}
	if server {
		conf.ClientCAs = roots
		conf.ClientAuth = tls.RequireAndVerifyClientCert
	} else {
		// Test certificates have SPIFFE URI SANs, not DNS SANs. Verify their
		// chains explicitly; the other side still performs SPIFFE authorization.
		conf.InsecureSkipVerify = true
		conf.VerifyConnection = func(state tls.ConnectionState) error {
			_, err := state.PeerCertificates[0].Verify(x509.VerifyOptions{Roots: roots, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}})
			return err
		}
	}
	return credentials.NewTLS(conf)
}
