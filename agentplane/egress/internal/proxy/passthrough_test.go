package proxy

import (
	"bufio"
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/internal/decision"
	transport "github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
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
	runtimeURI := "spiffe://agentplane/test/sandbox-uid/credential"
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
	lab.proxy.options.TLSPassthroughTargets = []string{authority}
	raw, response := lab.connect(t, authority, "X-Agentplane-Subject: test/attacker\r\n")
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
	_, refused := lab.connect(t, fixtureHost+":1", "")
	if refused.StatusCode == http.StatusOK {
		t.Fatal("unconfigured passthrough target admitted")
	}
	withoutSVID := lab.proxyClientTLS.Clone()
	withoutSVID.GetClientCertificate = nil
	withoutSVID.Certificates = nil
	noIdentity, err := tls.DialWithDialer(&net.Dialer{Timeout: 5 * time.Second}, "tcp", lab.address, withoutSVID)
	if err == nil {
		defer noIdentity.Close()
		if err := noIdentity.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
			t.Fatal(err)
		}
		if _, err := fmt.Fprintf(noIdentity, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", authority, authority); err == nil {
			answer, err := http.ReadResponse(bufio.NewReader(noIdentity), &http.Request{Method: http.MethodConnect})
			if err == nil && answer.StatusCode == http.StatusOK {
				t.Fatal("unauthenticated passthrough admitted")
			}
		}
	}
}

// failIdentityRead lets the real credential resolve until a specific live Identity lookup fails.
type failIdentityRead struct {
	client.Reader
	remaining int
	err       error
}

func (r *failIdentityRead) Get(ctx context.Context, key client.ObjectKey, object client.Object, opts ...client.GetOption) error {
	if _, ok := object.(*policyapi.Identity); ok {
		r.remaining--
		if r.remaining == 0 {
			return r.err
		}
	}
	return r.Reader.Get(ctx, key, object, opts...)
}

func TestPassthroughIdentityRefusal(t *testing.T) {
	for _, phase := range []struct {
		name string
		read int
	}{{"initial", 1}, {"post-dial", 2}} {
		for _, failure := range []struct {
			name   string
			err    error
			status int
			reason decision.Reason
		}{
			{"unavailable", context.DeadlineExceeded, http.StatusBadGateway, decision.Unavailable},
			{"deleted", apierrors.NewNotFound(schema.GroupResource{Resource: "identities"}, "test"), http.StatusForbidden, decision.TokenRejected},
		} {
			t.Run(phase.name+"/"+failure.name, func(t *testing.T) {
				lab := newLabWithIdentityReader(t, func(reader client.Reader) client.Reader {
					return &failIdentityRead{Reader: reader, remaining: phase.read, err: failure.err}
				})
				server, _ := lab.upstream(t, fixtureHost, false, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
					t.Error("rejected tunnel forwarded a request")
				}))
				authority := fixtureHost + ":" + portOf(server)
				lab.proxy.options.TLSPassthroughTargets = []string{authority}
				_, response := lab.connect(t, authority, "")
				defer response.Body.Close()
				if got := response.Header.Get(deniedHeader); response.StatusCode != failure.status || got != "denied; reason="+string(failure.reason) {
					t.Errorf("CONNECT returned %s, %s", response.Status, got)
				}
				records := lab.records.all()
				if len(records) != 1 || records[0].Outcome != decision.Deny || records[0].Reason == nil ||
					*records[0].Reason != failure.reason || records[0].Subject != nil {
					t.Fatalf("refusal audit = %+v", records)
				}
			})
		}
	}
}
