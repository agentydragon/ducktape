package proxy

import (
	"bufio"
	"crypto/tls"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
)

func TestPassthroughPreservesDestinationTLSWithoutPolicyOrCallerHeaders(t *testing.T) {
	lab := newLab(t)
	lab.allow() // The destination, rather than an interception rule, owns admission.
	ca := transporttesting.NewCA(t)
	serverCredentials := ca.Issue(t, transport.HistoryIngressURI)
	serverTLS, err := serverCredentials.WorkloadServerTLSConfig(transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	runtimeURI := "spiffe://agentplane/environment-runtime/test/sandbox-uid/credential"
	service := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.TLS == nil || len(r.TLS.PeerCertificates) == 0 || r.TLS.PeerCertificates[0].URIs[0].String() != runtimeURI {
			t.Error("destination did not receive the Runtime's certificate")
		}
		if r.Header.Get("Proxy-Authorization") != "" || r.Header.Get("X-Agentplane-Subject") != "" {
			t.Error("proxy asserted a caller to the destination")
		}
		_, _ = io.WriteString(w, "service-authenticated")
	}))
	// httptest injects its localhost certificate when Certificates is empty,
	// even with the shared transport's rotating GetCertificate callback.
	pair, err := tls.LoadX509KeyPair(serverCredentials.CertFile, serverCredentials.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.Certificates = []tls.Certificate{pair}
	service.TLS = serverTLS
	service.StartTLS()
	defer service.Close()
	_, port, err := net.SplitHostPort(service.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	authority := fixtureHost + ":" + port
	lab.options.TLSPassthroughTargets = []string{authority}
	lab.start(t)
	raw, response := lab.connect(t, authority, validBearer+"X-Agentplane-Subject: test/attacker\r\n")
	if response.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT: %s", response.Status)
	}
	clientCredentials := ca.Issue(t, runtimeURI)
	clientTLS, err := clientCredentials.ClientTLSConfig(transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	client := tls.Client(raw, clientTLS)
	if err := client.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := client.Handshake(); err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequest(http.MethodGet, "https://"+authority+"/private", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := request.Write(client); err != nil {
		t.Fatal(err)
	}
	answer, err := http.ReadResponse(bufio.NewReader(client), request)
	if err != nil {
		t.Fatal(err)
	}
	defer answer.Body.Close()
	body, err := io.ReadAll(answer.Body)
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != "service-authenticated" {
		t.Fatalf("response: %q", body)
	}
	_ = client.Close()

	// The exception is exact: neither an unconfigured authority nor an
	// unauthenticated outer hop can reach the destination.
	_, refused := lab.connect(t, fixtureHost+":1", validBearer)
	if refused.StatusCode == http.StatusOK {
		t.Fatal("unconfigured passthrough target admitted")
	}
	_, refused = lab.connect(t, authority, "")
	if refused.StatusCode == http.StatusOK {
		t.Fatal("unauthenticated passthrough admitted")
	}
}
