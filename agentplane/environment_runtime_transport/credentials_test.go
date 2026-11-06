package environment_runtime_transport_test

import (
	"bytes"
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
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "pod-uid")
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
	runtimeURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
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

func TestClientTLSConfigReloadsSameIdentityAndRejectsChangedIdentity(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runtimeURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	otherRuntimeURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "rotated-credential-id")
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
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	otherEnvironmentRuntimeURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "other-pod-uid")
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
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
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

func TestCredentialsRejectUntrustedRootsAndMissingEKU(t *testing.T) {
	trustedCA := environmentruntimetransporttesting.NewCA(t)
	otherCA := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "pod-uid")
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
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
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

func TestCredentialsReloadRotatedCertificateAndRoots(t *testing.T) {
	oldCA := environmentruntimetransporttesting.NewCA(t)
	newCA := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "pod-uid")
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
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
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
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "workload-credential")
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
	rotate()
	if wait := time.Until(oldCertificateNotAfter) + 50*time.Millisecond; wait > 0 {
		time.Sleep(wait)
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
	runnerURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
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

func TestParseEnvironmentRuntimeURIRejectsNonCanonicalAndMalformedPaths(t *testing.T) {
	valid, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "pod-uid")
	if err != nil {
		t.Fatal(err)
	}
	namespace, sandboxUID, workloadUID, err := environment_runtime_transport.ParseEnvironmentRuntimeURI(valid)
	if err != nil {
		t.Fatal(err)
	}
	if namespace != "agent-ns" || sandboxUID != "sandbox-uid" || workloadUID != "pod-uid" {
		t.Fatalf("parsed identity = (%q, %q, %q)", namespace, sandboxUID, workloadUID)
	}
	for _, invalid := range []string{
		"spiffe://agentplane/env/e2e/agent-ns/sandbox-uid",
		"spiffe://agentplane/env/e2e/agent-ns/sandbox-uid/pod-uid/extra",
		"spiffe://agentplane/env/e2e/Agent-ns/sandbox-uid/pod-uid",
		"spiffe://agentplane/env/e2e/agent-ns/sandbox%2Fuid/pod-uid",
		"spiffe://other/environment-runtime/agent-ns/sandbox-uid/pod-uid",
	} {
		if _, _, _, err := environment_runtime_transport.ParseEnvironmentRuntimeURI(invalid); err == nil {
			t.Errorf("ParseEnvironmentRuntimeURI accepted %q", invalid)
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
