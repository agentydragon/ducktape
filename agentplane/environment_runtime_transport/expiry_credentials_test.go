package environment_runtime_transport_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/http"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
)

func TestProxyTLSConnectionExpiresAndReloadsTheSameRuntimeIdentity(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	uri, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	short := ca.IssueWithValidity(t, uri, time.Now().Add(-time.Minute), time.Now().Add(4*time.Second), x509.ExtKeyUsageClientAuth)
	server := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := server.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	serials := make(chan string, 2)
	go func() {
		for {
			connection, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				defer connection.Close()
				secured := tls.Server(connection, serverTLS)
				if err := secured.Handshake(); err != nil {
					return
				}
				serials <- secured.ConnectionState().PeerCertificates[0].SerialNumber.String()
				_, _ = secured.Read(make([]byte, 1))
			}()
		}
	}()
	security, err := short.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	dial := func() net.Conn {
		t.Helper()
		ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
		defer cancel()
		raw, err := (&net.Dialer{}).DialContext(ctx, "tcp", listener.Addr().String())
		if err != nil {
			t.Fatal(err)
		}
		connection, err := environment_runtime_transport.ClientTLSConnection(ctx, raw, security)
		if err != nil {
			t.Fatal(err)
		}
		return connection
	}
	first := dial()
	defer first.Close()
	var firstSerial string
	select {
	case firstSerial = <-serials:
	case <-time.After(3 * time.Second):
		t.Fatal("first SVID was not accepted")
	}
	renewed := ca.IssueWithUsage(t, uri, x509.ExtKeyUsageClientAuth)
	for target, source := range map[string]string{short.CertFile: renewed.CertFile, short.KeyFile: renewed.KeyFile} {
		content, err := os.ReadFile(source)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, content, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	_ = first.SetReadDeadline(time.Now().Add(6 * time.Second))
	if _, err := first.Read(make([]byte, 1)); err == nil || isTimeout(err) {
		t.Fatalf("persistent proxy TLS connection survived selected certificate expiry: %v", err)
	}
	second := dial()
	defer second.Close()
	select {
	case secondSerial := <-serials:
		if secondSerial == firstSerial {
			t.Fatal("proxy reconnect reused the expired SVID")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("rotated SVID was not accepted")
	}
}

func TestHTTPServerTLSExpiryConnStateClosesAtLocalExpiryAndStopsOnHijack(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runtimeURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	runtime := ca.IssueWithUsage(t, runtimeURI, x509.ExtKeyUsageClientAuth)
	proxy := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := proxy.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS, err := runtime.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}

	t.Run("active connection closes at its selected local bound", func(t *testing.T) {
		client, server := handshakeTLSConnections(t, clientTLS, serverTLS)
		defer client.Close()
		defer server.Close()
		var callbackConn net.Conn
		connState := environment_runtime_transport.HTTPServerTLSExpiryConnState(func(conn *tls.Conn) time.Time {
			callbackConn = conn
			return time.Now().Add(120 * time.Millisecond)
		})
		connState(server, http.StateActive)
		if callbackConn != server {
			t.Fatal("HTTP expiry helper did not receive the original *tls.Conn")
		}
		_ = client.SetReadDeadline(time.Now().Add(time.Second))
		_, err := client.Read(make([]byte, 1))
		if err == nil || isTimeout(err) {
			t.Fatalf("client read after local expiry = %v; want the server to close the concrete TLS connection", err)
		}
		connState(server, http.StateClosed)
	})

	t.Run("hijacked connection is no longer closed by the server helper", func(t *testing.T) {
		client, server := handshakeTLSConnections(t, clientTLS, serverTLS)
		defer client.Close()
		defer server.Close()
		connState := environment_runtime_transport.HTTPServerTLSExpiryConnState(func(*tls.Conn) time.Time {
			return time.Now().Add(120 * time.Millisecond)
		})
		connState(server, http.StateActive)
		connState(server, http.StateHijacked)
		_ = client.SetReadDeadline(time.Now().Add(250 * time.Millisecond))
		_, err := client.Read(make([]byte, 1))
		if !isTimeout(err) {
			t.Fatalf("client read after hijack = %v; want timeout because StateHijacked stopped expiry closure", err)
		}
	})
}

func TestHTTPServerTLSExpiryConnStateUsesAuthenticatedPeerExpiry(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runtimeURI, err := environment_runtime_transport.EnvironmentRuntimeURI("agent-ns", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	runtime := ca.IssueWithValidity(t, runtimeURI, now.Add(-time.Minute), now.Add(900*time.Millisecond), x509.ExtKeyUsageClientAuth)
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
	defer client.Close()
	defer server.Close()
	connState := environment_runtime_transport.HTTPServerTLSExpiryConnState(nil)
	connState(server, http.StateActive)
	_ = client.SetReadDeadline(time.Now().Add(2 * time.Second))
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

func isTimeout(err error) bool {
	if err == nil {
		return false
	}
	if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
		return true
	}
	return false
}
