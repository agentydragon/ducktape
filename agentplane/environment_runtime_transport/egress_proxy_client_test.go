package environment_runtime_transport_test

import (
	"crypto/tls"
	"crypto/x509"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
)

func TestProxyServerTLSConfigVerifiesSPIFFEIdentityWithoutClientCertificate(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	serverCredential := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := serverCredential.WorkloadServerTLSConfigWithBearer(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS, err := (environment_runtime_transport.Config{CAFile: ca.RootFile}).ProxyServerTLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	clientState, serverState, err := tlsHandshake(t, clientTLS, serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	if identity, err := environment_runtime_transport.PeerIdentityFromTLSState(clientState, x509.ExtKeyUsageServerAuth); err != nil || identity != environment_runtime_transport.EgressProxyURI {
		t.Fatalf("server identity = %q, error %v", identity, err)
	}
	if len(serverState.PeerCertificates) != 0 {
		t.Fatalf("bearer TLS presented %d client certificates", len(serverState.PeerCertificates))
	}
}

func TestProxyServerTLSConfigRejectsWrongIdentityAndUnknownCA(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	wrongServerCredential := ca.IssueWithUsage(t, environment_runtime_transport.GatewayURI, x509.ExtKeyUsageServerAuth)
	wrongPair, err := tls.LoadX509KeyPair(wrongServerCredential.CertFile, wrongServerCredential.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	wrongServerTLS := &tls.Config{Certificates: []tls.Certificate{wrongPair}, ClientAuth: tls.RequestClientCert}
	clientTLS, err := (environment_runtime_transport.Config{CAFile: ca.RootFile}).ProxyServerTLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := tlsHandshake(t, clientTLS, wrongServerTLS); err == nil {
		t.Fatal("bearer TLS accepted a trusted server with the wrong SPIFFE identity")
	}

	correctServerCredential := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	correctServerTLS, err := correctServerCredential.WorkloadServerTLSConfigWithBearer(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	unknownCA := environmentruntimetransporttesting.NewCA(t)
	unknownClientTLS, err := (environment_runtime_transport.Config{CAFile: unknownCA.RootFile}).ProxyServerTLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := tlsHandshake(t, unknownClientTLS, correctServerTLS); err == nil {
		t.Fatal("bearer TLS accepted a server outside its configured CA bundle")
	}
}

func TestProxyServerTLSConfigRequiresCAOnly(t *testing.T) {
	if _, err := (environment_runtime_transport.Config{}).ProxyServerTLSConfig(); err == nil {
		t.Fatal("bearer TLS accepted a missing CA file")
	}
	if _, err := (environment_runtime_transport.Config{CertFile: "/cert", KeyFile: "/key", CAFile: "/ca"}).ProxyServerTLSConfig(); err == nil {
		t.Fatal("bearer TLS accepted client certificate fields")
	}
}
