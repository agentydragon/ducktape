package environment_runtime_transport_test

import (
	"crypto/tls"
	"crypto/x509"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
)

func TestClientTLSValidatesSupportedServerURIs(t *testing.T) {
	for _, uri := range []string{
		environment_runtime_transport.GatewayURI,
		environment_runtime_transport.EgressProxyURI,
		environment_runtime_transport.HistoryIngressURI,
		environment_runtime_transport.NotificationsURI,
		environment_runtime_transport.LLMIngressURI,
		environment_runtime_transport.SessionDispatcherURI,
		environment_runtime_transport.ActionsURI,
	} {
		t.Run(uri, func(t *testing.T) {
			if err := (environment_runtime_transport.ClientTLS{CAFile: "/test/ca.pem", ServerURI: uri}).Validate(); err != nil {
				t.Fatalf("Validate() error = %v", err)
			}
		})
	}
	for _, uri := range []string{"spiffe://agentplane/unknown", "spiffe://other/notifications", "https://agentplane/notifications"} {
		t.Run(uri, func(t *testing.T) {
			if err := (environment_runtime_transport.ClientTLS{CAFile: "/test/ca.pem", ServerURI: uri}).Validate(); err == nil {
				t.Fatal("Validate() accepted an unsupported service URI")
			}
		})
	}
}

func TestClientTLSVerifiesServiceIdentityServerAuthAndRootsWithoutClientCertificate(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	server := ca.IssueWithUsage(t, environment_runtime_transport.HistoryIngressURI, x509.ExtKeyUsageServerAuth)
	serverPair, err := tls.LoadX509KeyPair(server.CertFile, server.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS := &tls.Config{Certificates: []tls.Certificate{serverPair}}
	client := environment_runtime_transport.ClientTLS{CAFile: ca.RootFile, ServerURI: environment_runtime_transport.HistoryIngressURI}
	clientTLS, err := client.TLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	clientState, serverState, err := tlsHandshake(t, clientTLS, serverTLS)
	if err != nil {
		t.Fatalf("trusted server handshake: %v", err)
	}
	if len(serverState.PeerCertificates) != 0 {
		t.Fatalf("client presented %d certificates", len(serverState.PeerCertificates))
	}
	if len(clientState.PeerCertificates) != 1 {
		t.Fatalf("client saw %d peer certificates, want 1", len(clientState.PeerCertificates))
	}

	wrongIdentity := client
	wrongIdentity.ServerURI = environment_runtime_transport.NotificationsURI
	wrongIdentityTLS, err := wrongIdentity.TLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := tlsHandshake(t, wrongIdentityTLS, serverTLS); err == nil {
		t.Fatal("client accepted a trusted server with the wrong SPIFFE identity")
	}

	wrongEKU := ca.IssueWithUsage(t, environment_runtime_transport.HistoryIngressURI, x509.ExtKeyUsageClientAuth)
	wrongEKUPair, err := tls.LoadX509KeyPair(wrongEKU.CertFile, wrongEKU.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	wrongEKUTLS := &tls.Config{Certificates: []tls.Certificate{wrongEKUPair}}
	if _, _, err := tlsHandshake(t, clientTLS, wrongEKUTLS); err == nil {
		t.Fatal("client accepted a server certificate without serverAuth EKU")
	}

	forgedCA := environmentruntimetransporttesting.NewCA(t)
	forgedServer := forgedCA.IssueWithUsage(t, environment_runtime_transport.HistoryIngressURI, x509.ExtKeyUsageServerAuth)
	forgedPair, err := tls.LoadX509KeyPair(forgedServer.CertFile, forgedServer.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	forgedTLS := &tls.Config{Certificates: []tls.Certificate{forgedPair}}
	if _, _, err := tlsHandshake(t, clientTLS, forgedTLS); err == nil {
		t.Fatal("client accepted a server outside its configured trust roots")
	}
}

func TestClientTLSInvalidConfigurationFailsWithoutDowngrade(t *testing.T) {
	for name, config := range map[string]*environment_runtime_transport.ClientTLS{
		"missing CA":          {ServerURI: environment_runtime_transport.HistoryIngressURI},
		"unsupported service": {CAFile: "/test/ca.pem", ServerURI: "spiffe://agentplane/unknown"},
	} {
		t.Run(name, func(t *testing.T) {
			credentials, err := config.Credentials()
			if err == nil || credentials != nil {
				t.Fatalf("Credentials() = (%v, %v), want an error and no fallback credentials", credentials, err)
			}
		})
	}

	plaintext, err := (*environment_runtime_transport.ClientTLS)(nil).Credentials()
	if err != nil {
		t.Fatal(err)
	}
	if got := plaintext.Info().SecurityProtocol; got != "insecure" {
		t.Fatalf("nil ClientTLS protocol = %q, want explicit insecure transport", got)
	}
}
