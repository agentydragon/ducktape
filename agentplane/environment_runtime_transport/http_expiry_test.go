package environment_runtime_transport_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"net"
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
	if _, ok := first.(*tls.Conn); !ok {
		t.Fatalf("ClientTLSConnection returned %T; want the concrete TLS connection for HTTP transport", first)
	}
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

func TestClientTLSConnectionWithoutClientCertificateExpiresAtPeerNotAfter(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	proxy := ca.IssueWithValidity(
		t,
		environment_runtime_transport.EgressProxyURI,
		time.Now().Add(-time.Minute),
		time.Now().Add(1200*time.Millisecond),
		x509.ExtKeyUsageServerAuth,
	)
	pair, err := tls.LoadX509KeyPair(proxy.CertFile, proxy.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS := &tls.Config{Certificates: []tls.Certificate{pair}, ClientAuth: tls.RequestClientCert}
	clientTLS, err := (environment_runtime_transport.ClientTLS{CAFile: proxy.CAFile, ServerURI: environment_runtime_transport.EgressProxyURI}).TLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	listener, serverStates := startTLSServer(t, serverTLS)
	defer listener.Close()
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	raw, err := (&net.Dialer{}).DialContext(ctx, "tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	client, err := environment_runtime_transport.ClientTLSConnection(ctx, raw, clientTLS)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	select {
	case state := <-serverStates:
		if len(state.PeerCertificates) != 0 {
			t.Fatalf("bearer TLS presented %d peer certificates; want none", len(state.PeerCertificates))
		}
	case <-time.After(3 * time.Second):
		t.Fatal("bearer TLS handshake did not complete")
	}
	_ = client.SetReadDeadline(time.Now().Add(3 * time.Second))
	if _, err := client.Read(make([]byte, 1)); err == nil || isTimeout(err) {
		t.Fatalf("bearer TLS connection survived authenticated peer certificate expiry: %v", err)
	}
}

func TestClientTLSConnectionRejectsConfiguredClientCertificateWithoutSelection(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	proxy := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	pair, err := tls.LoadX509KeyPair(proxy.CertFile, proxy.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS := &tls.Config{Certificates: []tls.Certificate{pair}, ClientAuth: tls.RequestClientCert}
	clientTLS, err := (environment_runtime_transport.ClientTLS{CAFile: proxy.CAFile, ServerURI: environment_runtime_transport.EgressProxyURI}).TLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	callbackCalled := false
	clientTLS.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
		callbackCalled = true
		return nil, nil
	}
	listener, _ := startTLSServer(t, serverTLS)
	defer listener.Close()
	ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
	defer cancel()
	raw, err := (&net.Dialer{}).DialContext(ctx, "tcp", listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	client, err := environment_runtime_transport.ClientTLSConnection(ctx, raw, clientTLS)
	if client != nil {
		_ = client.Close()
		t.Fatal("TLS connection succeeded without the configured client certificate")
	}
	if err == nil {
		t.Fatal("TLS connection succeeded without the configured client certificate")
	}
	if !callbackCalled {
		t.Fatal("TLS server did not request the configured client certificate")
	}
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

func startTLSServer(t *testing.T, config *tls.Config) (net.Listener, <-chan tls.ConnectionState) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	states := make(chan tls.ConnectionState, 1)
	go func() {
		raw, err := listener.Accept()
		if err != nil {
			return
		}
		defer raw.Close()
		secured := tls.Server(raw, config)
		if err := secured.Handshake(); err != nil {
			return
		}
		states <- secured.ConnectionState()
		_, _ = secured.Read(make([]byte, 1))
	}()
	return listener, states
}
