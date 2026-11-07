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

func TestHTTPServerTLSExpiryConnStateUsesAuthenticatedPeerExpiry(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runtimeURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	// X.509 encodes NotAfter at whole-second precision; leave time for setup
	// after that truncation before testing the connection's expiry.
	expires := now.Truncate(time.Second).Add(3 * time.Second)
	runtime := ca.IssueWithValidity(t, runtimeURI, now.Add(-time.Minute), expires, x509.ExtKeyUsageClientAuth)
	proxy := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := proxy.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS, err := runtime.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	client, server := handshakeTLSConnections(t, clientTLS, serverTLS)
	connState := environment_runtime_transport.HTTPServerTLSExpiryConnState()
	connState(server, http.StateActive)
	_ = client.SetReadDeadline(expires.Add(2 * time.Second))
	_, err = client.Read(make([]byte, 1))
	if err == nil || isTimeout(err) {
		t.Fatalf("client read after peer expiry = %v; want the server to close at authenticated peer NotAfter", err)
	}
	connState(server, http.StateClosed)
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
