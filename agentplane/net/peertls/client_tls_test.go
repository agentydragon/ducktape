package peertls_test

import (
	"crypto/tls"
	"crypto/x509"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
)

func TestClientTLSValidatesSupportedServerURIs(t *testing.T) {
	for _, uri := range []string{
		peertls.RunnerGatewayURI,
		peertls.EgressProxyURI,
		peertls.HistoryIngressURI,
		peertls.NotificationsURI,
		peertls.LLMIngressURI,
		peertls.SessionDispatcherURI,
		peertls.ActionsURI,
		peertls.PolicyControllerURI,
		peertls.CentralSamplerURI,
	} {
		t.Run(uri, func(t *testing.T) {
			if err := (peertls.ClientTLS{CAFile: "/test/ca.pem", ServerURI: uri}).Validate(); err != nil {
				t.Fatalf("Validate() error = %v", err)
			}
		})
	}
	for _, uri := range []string{"spiffe://agentplane/unknown", "spiffe://other/notifications", "https://agentplane/notifications"} {
		t.Run(uri, func(t *testing.T) {
			if err := (peertls.ClientTLS{CAFile: "/test/ca.pem", ServerURI: uri}).Validate(); err == nil {
				t.Fatal("Validate() accepted an unsupported service URI")
			}
		})
	}
}

func TestClientTLSVerifiesServiceIdentityServerAuthAndRootsWithoutClientCertificate(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	server := ca.IssueWithUsage(t, peertls.HistoryIngressURI, x509.ExtKeyUsageServerAuth)
	serverPair, err := tls.LoadX509KeyPair(server.CertFile, server.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS := &tls.Config{Certificates: []tls.Certificate{serverPair}}
	client := peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.HistoryIngressURI}
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
	wrongIdentity.ServerURI = peertls.NotificationsURI
	wrongIdentityTLS, err := wrongIdentity.TLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := tlsHandshake(t, wrongIdentityTLS, serverTLS); err == nil {
		t.Fatal("client accepted a trusted server with the wrong SPIFFE identity")
	}

	wrongEKU := ca.IssueWithUsage(t, peertls.HistoryIngressURI, x509.ExtKeyUsageClientAuth)
	wrongEKUPair, err := tls.LoadX509KeyPair(wrongEKU.CertFile, wrongEKU.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	wrongEKUTLS := &tls.Config{Certificates: []tls.Certificate{wrongEKUPair}}
	if _, _, err := tlsHandshake(t, clientTLS, wrongEKUTLS); err == nil {
		t.Fatal("client accepted a server certificate without serverAuth EKU")
	}

	forgedCA := environmentruntimetransporttesting.NewCA(t)
	forgedServer := forgedCA.IssueWithUsage(t, peertls.HistoryIngressURI, x509.ExtKeyUsageServerAuth)
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
	for name, config := range map[string]*peertls.ClientTLS{
		"missing CA":          {ServerURI: peertls.HistoryIngressURI},
		"unsupported service": {CAFile: "/test/ca.pem", ServerURI: "spiffe://agentplane/unknown"},
	} {
		t.Run(name, func(t *testing.T) {
			credentials, err := config.Credentials()
			if err == nil || credentials != nil {
				t.Fatalf("Credentials() = (%v, %v), want an error and no fallback credentials", credentials, err)
			}
		})
	}

	plaintext, err := (*peertls.ClientTLS)(nil).Credentials()
	if err != nil {
		t.Fatal(err)
	}
	if got := plaintext.Info().SecurityProtocol; got != "insecure" {
		t.Fatalf("nil ClientTLS protocol = %q, want explicit insecure transport", got)
	}
}

func TestServiceSVIDCallerValidation(t *testing.T) {
	valid := peertls.ClientTLS{CAFile: "/ca", ServerURI: peertls.ActionsURI, CertFile: "/cert", KeyFile: "/key", ClientURI: peertls.AppURI}
	if err := valid.ValidateCaller("", peertls.AppURI); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*peertls.ClientTLS){
		"missing cert":      func(c *peertls.ClientTLS) { c.CertFile = "" },
		"relative key":      func(c *peertls.ClientTLS) { c.KeyFile = "key" },
		"missing identity":  func(c *peertls.ClientTLS) { c.ClientURI = "" },
		"workload identity": func(c *peertls.ClientTLS) { c.ClientURI = "spiffe://agentplane/ns/test/sandbox/agent/uid/uid" },
		"other role":        func(c *peertls.ClientTLS) { c.ClientURI = peertls.MCPEdgeURI },
	} {
		t.Run(name, func(t *testing.T) {
			invalid := valid
			mutate(&invalid)
			if err := invalid.ValidateCaller("", peertls.AppURI); err == nil {
				t.Fatal("accepted invalid SVID caller")
			}
		})
	}
	if err := valid.ValidateCaller("/token", peertls.AppURI); err == nil {
		t.Fatal("accepted mixed SVID and bearer")
	}
}

func TestClientSVIDPinsIdentityAndReloadsCertificate(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	server := ca.Issue(t, peertls.ActionsURI)
	serverTLS, err := server.WorkloadServerTLSConfigWithServices(peertls.ActionsURI, false)
	if err != nil {
		t.Fatal(err)
	}
	leaf := ca.Issue(t, peertls.AppURI)
	client := peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.ActionsURI, CertFile: leaf.CertFile, KeyFile: leaf.KeyFile, ClientURI: peertls.AppURI}
	clientTLS, err := client.TLSConfig()
	if err != nil {
		t.Fatal(err)
	}
	check := func() []byte {
		t.Helper()
		_, peer, err := tlsHandshake(t, clientTLS, serverTLS)
		if err != nil {
			t.Fatal(err)
		}
		if len(peer.PeerCertificates) != 1 || peer.PeerCertificates[0].URIs[0].String() != peertls.AppURI {
			t.Fatal("wrong client peer")
		}
		return peer.PeerCertificates[0].Raw
	}
	first := string(check())
	replace := func(replacement peertls.Config) {
		t.Helper()
		for dst, src := range map[string]string{leaf.CertFile: replacement.CertFile, leaf.KeyFile: replacement.KeyFile} {
			data, err := os.ReadFile(src)
			if err != nil {
				t.Fatal(err)
			}
			// Atomic replacement matches the projected volume's inode changes.
			if err := os.WriteFile(dst+".new", data, 0600); err != nil {
				t.Fatal(err)
			}
			if err := os.Rename(dst+".new", dst); err != nil {
				t.Fatal(err)
			}
		}
	}
	replace(ca.Issue(t, peertls.AppURI))
	if string(check()) == first {
		t.Fatal("client certificate did not rotate")
	}
	replace(ca.Issue(t, peertls.MCPEdgeURI))
	if _, _, err := tlsHandshake(t, clientTLS, serverTLS); err == nil {
		t.Fatal("accepted changed local identity")
	}
	if _, err := client.TLSConfig(); err == nil {
		t.Fatal("accepted certificate not matching configured client_uri")
	}
	replace(ca.IssueWithUsage(t, peertls.AppURI, x509.ExtKeyUsageServerAuth))
	if _, err := client.Credentials(); err == nil {
		t.Fatal("accepted local leaf without clientAuth")
	}
	if err := os.Remove(leaf.CertFile); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Credentials(); err == nil {
		t.Fatal("missing certificate fell back")
	}
}
