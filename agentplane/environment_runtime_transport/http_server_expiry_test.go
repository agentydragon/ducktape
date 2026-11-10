package environment_runtime_transport_test

import (
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
)

func TestHTTPServerTLSExpiryConnStateUsesEarlierLeafExpiry(t *testing.T) {
	for _, tc := range []struct {
		name          string
		localLifetime time.Duration
		peerLifetime  time.Duration
		bearerOnly    bool
	}{
		{name: "peer expires first", localLifetime: 8 * time.Second, peerLifetime: 3 * time.Second},
		{name: "local expires first", localLifetime: 3 * time.Second, peerLifetime: 8 * time.Second},
		{name: "bearer only local expiry", localLifetime: 3 * time.Second, bearerOnly: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client, server, connState, expiresAt := establishHTTPExpiryConnection(t, tc.localLifetime, tc.peerLifetime, tc.bearerOnly)
			connState(server, http.StateActive)
			assertClosesAtExpiry(t, client, expiresAt)
			connState(server, http.StateClosed)
		})
	}
}

func TestHTTPServerTLSExpiryConnStateUsesHandshakeSelectedLeafAfterRotation(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	localURI := environment_runtime_transport.EgressProxyURI
	runtimeURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Second)
	selectedExpiry := now.Add(3 * time.Second)
	peerExpiry := now.Add(8 * time.Second)
	local := ca.IssueWithValidity(t, localURI, now.Add(-time.Minute), selectedExpiry, x509.ExtKeyUsageServerAuth)
	renewed := ca.IssueWithUsage(t, localURI, x509.ExtKeyUsageServerAuth)
	runtime := ca.IssueWithValidity(t, runtimeURI, now.Add(-time.Minute), peerExpiry, x509.ExtKeyUsageClientAuth)
	serverTLS, err := local.WorkloadServerTLSConfig(localURI)
	if err != nil {
		t.Fatal(err)
	}
	connState, err := environment_runtime_transport.ConfigureHTTPServerTLSExpiry(serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS, err := runtime.ClientTLSConfig(localURI)
	if err != nil {
		t.Fatal(err)
	}
	client, server := handshakeTLSConnections(t, clientTLS, serverTLS)
	if got := client.ConnectionState().PeerCertificates[0].NotAfter; !got.Equal(selectedExpiry) {
		t.Fatalf("handshake selected server leaf expires at %s, want %s", got, selectedExpiry)
	}
	copyFile(t, renewed.CertFile, local.CertFile)
	copyFile(t, renewed.KeyFile, local.KeyFile)
	connState(server, http.StateActive)
	assertClosesAtExpiry(t, client, selectedExpiry)
	connState(server, http.StateClosed)
}

func TestConfigureHTTPServerTLSExpiryDisablesSessionResumption(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	localURI := environment_runtime_transport.EgressProxyURI
	runtimeURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	local := ca.IssueWithUsage(t, localURI, x509.ExtKeyUsageServerAuth)
	runtime := ca.IssueWithUsage(t, runtimeURI, x509.ExtKeyUsageClientAuth)
	serverTLS, err := local.WorkloadServerTLSConfig(localURI)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.SessionTicketsDisabled = false
	// Keep the ticket key stable across tls.Server configs. Without the expiry
	// hook's setting, the shared client cache would otherwise resume this session.
	for i := range serverTLS.SessionTicketKey {
		serverTLS.SessionTicketKey[i] = byte(i + 1)
	}
	selections := 0
	getCertificate := serverTLS.GetCertificate
	serverTLS.GetCertificate = func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
		selections++
		return getCertificate(hello)
	}
	connState, err := environment_runtime_transport.ConfigureHTTPServerTLSExpiry(serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS, err := runtime.ClientTLSConfig(localURI)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS.ServerName = "egress-proxy.test"
	clientTLS.ClientSessionCache = tls.NewLRUClientSessionCache(1)
	for i := 0; i < 2; i++ {
		client, server := handshakeTLSConnections(t, clientTLS, serverTLS)
		if _, err := server.Write([]byte("ticket")); err != nil {
			t.Fatalf("server write for TLS ticket processing: %v", err)
		}
		if _, err := client.Read(make([]byte, len("ticket"))); err != nil {
			t.Fatalf("client read for TLS ticket processing: %v", err)
		}
		if client.ConnectionState().DidResume {
			t.Fatalf("handshake %d resumed a TLS session", i+1)
		}
		connState(server, http.StateActive)
		connState(server, http.StateClosed)
	}
	if selections != 2 {
		t.Fatalf("GetCertificate called %d times for two handshakes", selections)
	}
}

func TestConfigureHTTPServerTLSExpiryRejectsUntrackedCertificateSelection(t *testing.T) {
	for _, config := range []*tls.Config{
		{},
		{Certificates: []tls.Certificate{{}}},
		{GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) { return nil, nil }, GetConfigForClient: func(*tls.ClientHelloInfo) (*tls.Config, error) { return nil, nil }},
	} {
		if _, err := environment_runtime_transport.ConfigureHTTPServerTLSExpiry(config); err == nil {
			t.Fatalf("ConfigureHTTPServerTLSExpiry(%+v) succeeded without an observable certificate selector", config)
		}
	}
}

func establishHTTPExpiryConnection(
	t *testing.T,
	localLifetime, peerLifetime time.Duration,
	bearerOnly bool,
) (*tls.Conn, *tls.Conn, func(net.Conn, http.ConnState), time.Time) {
	t.Helper()
	ca := environmentruntimetransporttesting.NewCA(t)
	localURI := environment_runtime_transport.EgressProxyURI
	now := time.Now().UTC().Truncate(time.Second)
	localExpiry := now.Add(localLifetime)
	local := ca.IssueWithValidity(t, localURI, now.Add(-time.Minute), localExpiry, x509.ExtKeyUsageServerAuth)
	var peerExpiry time.Time
	var clientTLS *tls.Config
	var err error
	if bearerOnly {
		clientTLS, err = (environment_runtime_transport.ClientTLS{
			CAFile: ca.RootFile, ServerURI: localURI,
		}).TLSConfig()
		if err != nil {
			t.Fatal(err)
		}
	} else {
		runtimeURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
		if err != nil {
			t.Fatal(err)
		}
		peerExpiry = now.Add(peerLifetime)
		runtime := ca.IssueWithValidity(t, runtimeURI, now.Add(-time.Minute), peerExpiry, x509.ExtKeyUsageClientAuth)
		clientTLS, err = runtime.ClientTLSConfig(localURI)
		if err != nil {
			t.Fatal(err)
		}
	}
	var serverTLS *tls.Config
	if bearerOnly {
		serverTLS, err = local.WorkloadServerTLSConfigWithBearer(localURI)
	} else {
		serverTLS, err = local.WorkloadServerTLSConfig(localURI)
	}
	if err != nil {
		t.Fatal(err)
	}
	connState, err := environment_runtime_transport.ConfigureHTTPServerTLSExpiry(serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	client, server := handshakeTLSConnections(t, clientTLS, serverTLS)
	if bearerOnly {
		if len(server.ConnectionState().PeerCertificates) != 0 {
			t.Fatalf("bearer-only TLS received %d peer certificates, want none", len(server.ConnectionState().PeerCertificates))
		}
	} else if len(server.ConnectionState().PeerCertificates) == 0 {
		t.Fatal("mTLS handshake received no client certificate")
	}
	expiresAt := localExpiry
	if !peerExpiry.IsZero() && peerExpiry.Before(expiresAt) {
		expiresAt = peerExpiry
	}
	return client, server, connState, expiresAt
}

func assertClosesAtExpiry(t *testing.T, client *tls.Conn, expiresAt time.Time) {
	t.Helper()
	if err := client.SetReadDeadline(expiresAt.Add(2 * time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Read(make([]byte, 1)); err == nil || isTimeout(err) {
		t.Fatalf("HTTP TLS connection survived leaf expiry %s: %v", expiresAt, err)
	}
}

func handshakeTLSConnections(t *testing.T, clientConfig, serverConfig *tls.Config) (*tls.Conn, *tls.Conn) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	clientRaw, err := net.Dial("tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	serverRaw, err := listener.Accept()
	if err != nil {
		_ = clientRaw.Close()
		t.Fatal(err)
	}
	client := tls.Client(clientRaw, clientConfig)
	server := tls.Server(serverRaw, serverConfig)
	serverHandshake := make(chan error, 1)
	go func() { serverHandshake <- server.Handshake() }()
	clientErr := client.Handshake()
	serverErr := <-serverHandshake
	if clientErr != nil || serverErr != nil {
		_ = client.Close()
		_ = server.Close()
		t.Fatalf("TLS handshake: client %v; server %v", clientErr, serverErr)
	}
	t.Cleanup(func() {
		_ = client.Close()
		_ = server.Close()
	})
	return client, server
}
