package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
)

func TestWorkloadHTTPDefaultPortRoutePresentsSVIDThroughOpaqueConnectAndStreams(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	uri, err := environment_runtime_transport.EnvironmentRuntimeURI("test", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	credential := ca.Issue(t, uri)
	release := make(chan struct{})
	defer close(release)
	var calls atomic.Int32
	destination := workloadDestination(t, ca, environment_runtime_transport.EgressRulesURI, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		identity, err := environment_runtime_transport.PeerIdentityFromTLSState(*r.TLS, x509.ExtKeyUsageClientAuth)
		if err != nil || identity != uri || r.ProtoMajor != 2 {
			t.Errorf("destination peer=%q HTTP/%d error=%v", identity, r.ProtoMajor, err)
			http.Error(w, "bad peer", 400)
			return
		}
		if r.Header.Get("Authorization") != "" || r.Header.Get("Proxy-Authorization") != "" || r.Header.Get(projectedTokenHeader) != "" {
			t.Error("caller credential header reached destination")
		}
		calls.Add(1)
		w.Header().Set("Content-Type", "text/event-stream")
		io.WriteString(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	central, observed := workloadCentral(t, ca, uri, strings.TrimPrefix(destination.URL, "https://"))
	address := workloadSidecar(t, central, credential, workloadRouteConfig{Upstream: strings.TrimPrefix(destination.URL, "https://"), ServerURI: environment_runtime_transport.EgressRulesURI, TLS: credential})
	proxyURL, err := url.Parse("http://" + address)
	if err != nil {
		t.Fatal(err)
	}
	transport := &http.Transport{Proxy: http.ProxyURL(proxyURL)}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, "http://rules.test.invalid/watch", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer forged")
	request.Header.Set("Proxy-Authorization", "Bearer forged-hop")
	request.Header.Set(projectedTokenHeader, "audience forged-projection")
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	reader := bufio.NewReader(response.Body)
	line, err := reader.ReadString('\n')
	if err != nil || line != "data: first\n" || response.StatusCode != 200 || calls.Load() != 1 {
		t.Fatalf("first streamed line=%q status=%d calls=%d err=%v", line, response.StatusCode, calls.Load(), err)
	}
	// A complete SSE body must reach EOF even while the proxy connection is reusable.
	release <- struct{}{}
	remainder, err := io.ReadAll(reader)
	if err != nil || string(remainder) != "\n" {
		t.Fatalf("stream completion=%q error=%v", remainder, err)
	}

	select {
	case hop := <-observed:
		if hop.Method != http.MethodConnect || hop.Host != strings.TrimPrefix(destination.URL, "https://") || hop.Header.Get("Proxy-Authorization") != "" || hop.Header.Get(projectedTokenHeader) != "" {
			t.Fatalf("central CONNECT=%+v", hop)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("central did not see CONNECT")
	}
}

func TestWorkloadRouteDoesNotRetryWrongServiceIdentityAsPlaintext(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	uri, err := environment_runtime_transport.EnvironmentRuntimeURI("test", "sandbox-uid", "credential-id")
	if err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	destination := workloadDestination(t, ca, environment_runtime_transport.EgressProxyURI, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(200) }))
	credential := ca.Issue(t, uri)
	central, observed := workloadCentral(t, ca, uri, strings.TrimPrefix(destination.URL, "https://"))
	address := workloadSidecar(t, central, credential, workloadRouteConfig{Upstream: strings.TrimPrefix(destination.URL, "https://"), ServerURI: environment_runtime_transport.EgressRulesURI, TLS: credential})
	proxyURL, err := url.Parse("http://" + address)
	if err != nil {
		t.Fatal(err)
	}
	transport := &http.Transport{Proxy: http.ProxyURL(proxyURL)}
	defer transport.CloseIdleConnections()
	response, err := (&http.Client{Transport: transport, Timeout: 5 * time.Second}).Get("http://rules.test.invalid:80/forged")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusBadGateway || calls.Load() != 0 {
		t.Fatalf("wrong service identity reached handler: status=%d calls=%d", response.StatusCode, calls.Load())
	}
	select {
	case hop := <-observed:
		if hop.Method != http.MethodConnect {
			t.Fatal("not a CONNECT")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no CONNECT")
	}
	select {
	case extra := <-observed:
		t.Fatalf("TLS failure retried: %s", extra.Method)
	default:
	}
}

func workloadDestination(t *testing.T, ca *environmentruntimetransporttesting.CA, uri string, handler http.Handler) *httptest.Server {
	t.Helper()
	config := ca.IssueWithUsage(t, uri, x509.ExtKeyUsageServerAuth)
	security, err := config.WorkloadServerTLSConfig(uri)
	if err != nil {
		t.Fatal(err)
	}
	pair, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	security.Certificates = []tls.Certificate{pair}
	server := httptest.NewUnstartedServer(handler)
	server.TLS = security
	server.EnableHTTP2 = true
	server.StartTLS()
	t.Cleanup(server.Close)
	return server
}

func workloadCentral(t *testing.T, ca *environmentruntimetransporttesting.CA, clientURI, target string) (*httptest.Server, <-chan *http.Request) {
	t.Helper()
	requests := make(chan *http.Request, 8)
	serverCredential := ca.Issue(t, environment_runtime_transport.EgressProxyURI)
	security, err := serverCredential.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	pair, err := tls.LoadX509KeyPair(serverCredential.CertFile, serverCredential.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	security.Certificates = []tls.Certificate{pair}
	rootPEM, err := os.ReadFile(ca.RootFile)
	if err != nil {
		t.Fatal(err)
	}
	security.ClientCAs = x509.NewCertPool()
	if !security.ClientCAs.AppendCertsFromPEM(rootPEM) {
		t.Fatal("failed to load workload CA for central proxy client verification")
	}
	security.ClientAuth = tls.RequireAndVerifyClientCert
	security.NextProtos = []string{"http/1.1"}
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests <- r.Clone(r.Context())
		if r.TLS == nil {
			t.Error("central proxy request did not use TLS")
			http.Error(w, "TLS required", http.StatusForbidden)
			return
		}
		identity, err := environment_runtime_transport.PeerIdentityFromTLSState(*r.TLS, x509.ExtKeyUsageClientAuth)
		if err != nil || identity != clientURI {
			t.Errorf("central proxy client identity=%q, want %q: %v", identity, clientURI, err)
			http.Error(w, "wrong peer", http.StatusForbidden)
			return
		}
		if r.Method != http.MethodConnect || r.Host != target {
			http.Error(w, "unexpected target", http.StatusForbidden)
			return
		}
		upstream, err := net.DialTimeout("tcp", target, 3*time.Second)
		if err != nil {
			http.Error(w, "destination unavailable", 502)
			return
		}
		defer upstream.Close()
		downstream, buffer, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		defer downstream.Close()
		io.WriteString(downstream, "HTTP/1.1 200 Connection established\r\n\r\n")
		ended := make(chan struct{}, 2)
		go func() { io.Copy(upstream, buffer); ended <- struct{}{} }()
		go func() { io.Copy(downstream, upstream); ended <- struct{}{} }()
		<-ended
		upstream.Close()
		downstream.Close()
		<-ended
	}))
	server.TLS = security
	server.EnableHTTP2 = false
	server.StartTLS()
	t.Cleanup(server.Close)
	return server, requests
}

func workloadSidecar(t *testing.T, central *httptest.Server, proxyCredential environment_runtime_transport.Config, route workloadRouteConfig) string {
	t.Helper()
	centralURL, err := url.Parse(central.URL)
	if err != nil {
		t.Fatal(err)
	}
	host, port, err := net.SplitHostPort(centralURL.Host)
	if err != nil {
		t.Fatal(err)
	}
	number, err := strconv.Atoi(port)
	if err != nil {
		t.Fatal(err)
	}
	sidecar, err := newSidecar(config{
		ProxyHost: host, ProxyPort: number, ProxyMode: "mtls",
		ProxyTLS:       &proxyTLSConfig{CertFile: proxyCredential.CertFile, KeyFile: proxyCredential.KeyFile, CAFile: proxyCredential.CAFile},
		PlatformRoutes: map[string]platformRouteConfig{},
		WorkloadRoutes: map[string]workloadRouteConfig{"rules.test.invalid:80": route},
		ListenAddress:  "127.0.0.1:0", ReadinessListenAddress: "127.0.0.1:0",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := sidecar.prepareWorkloadRoutes(); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	readiness, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		listener.Close()
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- sidecar.serve(ctx, listener, readiness, nil) }()
	t.Cleanup(func() {
		cancel()
		sidecar.closeWorkloadRoutes()
		select {
		case err := <-done:
			if err != nil {
				t.Error(err)
			}
		case <-time.After(5 * time.Second):
			t.Error("sidecar did not stop")
		}
	})
	return listener.Addr().String()
}
