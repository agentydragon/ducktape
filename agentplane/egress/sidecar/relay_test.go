package main

import (
	"bufio"
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
)

const (
	testUpgradeProtocol = "echo.test"
)

type testProxyRequest struct {
	method  string
	target  string
	headers http.Header
	body    string
	// peer is the SPIFFE URI the hop's client certificate named.
	peer string
}

// testCentralProxy terminates the hop's mTLS as the egress proxy and answers each request with the
// peer it authenticated.
type testCentralProxy struct {
	listener net.Listener
	// client is the workload SVID a test sidecar presents, and peer its URI.
	client      environment_runtime_transport.Config
	peer        string
	requests    chan testProxyRequest
	connections atomic.Int64
	acceptDone  chan struct{}

	mu       sync.Mutex
	active   map[net.Conn]struct{}
	handlers sync.WaitGroup
}

func startTestCentralProxy(t *testing.T) *testCentralProxy {
	t.Helper()
	ca := environmentruntimetransporttesting.NewCA(t)
	server := ca.IssueWithUsage(t, environment_runtime_transport.EgressProxyURI, x509.ExtKeyUsageServerAuth)
	security, err := server.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	certificate, err := tls.LoadX509KeyPair(server.CertFile, server.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	security.Certificates = []tls.Certificate{certificate}
	peer, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	proxy := &testCentralProxy{
		listener:   tls.NewListener(listener, security),
		client:     ca.IssueWithUsage(t, peer, x509.ExtKeyUsageClientAuth),
		peer:       peer,
		requests:   make(chan testProxyRequest, 32),
		acceptDone: make(chan struct{}),
		active:     make(map[net.Conn]struct{}),
	}
	t.Cleanup(proxy.close)
	go func() {
		defer close(proxy.acceptDone)
		proxy.accept()
	}()
	return proxy
}

func (p *testCentralProxy) accept() {
	for {
		conn, err := p.listener.Accept()
		if err != nil {
			return
		}
		p.connections.Add(1)
		p.mu.Lock()
		p.active[conn] = struct{}{}
		p.mu.Unlock()
		p.handlers.Add(1)
		go p.handle(conn)
	}
}

func (p *testCentralProxy) handle(conn net.Conn) {
	defer p.handlers.Done()
	defer func() {
		p.mu.Lock()
		delete(p.active, conn)
		p.mu.Unlock()
		_ = conn.Close()
	}()
	tlsConn := conn.(*tls.Conn)
	if err := tlsConn.Handshake(); err != nil {
		return
	}
	peer, err := environment_runtime_transport.PeerIdentityFromTLSState(tlsConn.ConnectionState(), x509.ExtKeyUsageClientAuth)
	if err != nil {
		return
	}
	reader := bufio.NewReader(conn)
	for {
		request, err := http.ReadRequest(reader)
		if err != nil {
			return
		}
		if request.URL.Path == "/continue" && request.Header.Get("Expect") == "100-continue" {
			if _, err := io.WriteString(conn, "HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\nHTTP/1.1 100 Continue\r\n\r\n"); err != nil {
				return
			}
		}
		body, err := io.ReadAll(request.Body)
		if err != nil {
			return
		}
		observed := testProxyRequest{
			method:  request.Method,
			target:  request.RequestURI,
			headers: request.Header.Clone(),
			body:    string(body),
			peer:    peer,
		}
		p.requests <- observed
		if observed.method == http.MethodConnect {
			if strings.HasPrefix(observed.target, "refused.test") {
				_, _ = io.WriteString(conn, "HTTP/1.1 403 Forbidden\r\nx-agentplane-egress: denied; reason=no-rule\r\n\r\n")
				return
			}
			if _, err := io.WriteString(conn, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
				return
			}
			if strings.HasPrefix(observed.target, "half-close.test:") {
				_, _ = io.Copy(io.Discard, reader)
				_, _ = io.WriteString(conn, "response-after-half-close")
				return
			}
			_, _ = io.Copy(conn, reader)
			return
		}
		if request.Header.Get("Upgrade") == testUpgradeProtocol {
			if _, err := io.WriteString(conn, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: "+testUpgradeProtocol+"\r\n\r\n"); err != nil {
				return
			}
			_, _ = io.Copy(conn, reader)
			return
		}
		response := "seen-peer=" + peer
		connection := "keep-alive"
		if request.Close {
			connection = "close"
		}
		if _, err := fmt.Fprintf(conn, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: %s\r\n\r\n%s", len(response), connection, response); err != nil || request.Close {
			return
		}
	}
}

func (p *testCentralProxy) close() {
	_ = p.listener.Close()
	<-p.acceptDone
	p.mu.Lock()
	for conn := range p.active {
		_ = conn.Close()
	}
	p.mu.Unlock()
	p.handlers.Wait()
}

func (p *testCentralProxy) nextRequest(t *testing.T) testProxyRequest {
	t.Helper()
	select {
	case request := <-p.requests:
		return request
	case <-time.After(5 * time.Second):
		t.Fatal("central proxy did not receive a request")
		return testProxyRequest{}
	}
}

func readTestHead(reader *bufio.Reader) ([]byte, error) {
	var head []byte
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return nil, err
		}
		head = append(head, line...)
		if line == "\r\n" {
			return head, nil
		}
	}
}

func startTestSidecar(t *testing.T, central *testCentralProxy) string {
	_, address := startTestSidecarWithState(t, central, central.listener.Addr().String())
	return address
}

// startTestSidecarWithState presents central's client SVID to proxyAddress.
func startTestSidecarWithState(t *testing.T, central *testCentralProxy, proxyAddress string) (*sidecar, string) {
	t.Helper()
	s, err := newSidecar(config{
		Proxy: proxyConfig{
			Address: proxyAddress,
			Transport: tlsProxyTransportConfig{
				CAFile: central.client.CAFile,
				Auth:   spiffeProxyAuth{CertFile: central.client.CertFile, KeyFile: central.client.KeyFile},
			},
		},
		PlatformRoutes:         &platformRoutesConfig{Routes: map[string]platformRouteConfig{}},
		WorkloadRoutes:         map[string]workloadRouteConfig{},
		ListenAddress:          "127.0.0.1:0",
		ReadinessListenAddress: "127.0.0.1:0",
	})
	if err != nil {
		t.Fatalf("configure test sidecar: %v", err)
	}
	address := serveTestSidecar(t, s)
	return s, address
}

func serveTestSidecar(t *testing.T, s *sidecar) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s.proxyListener = listener
	s.proxyServer = &http.Server{Handler: s.proxyHandler(), MaxHeaderBytes: maxHeadBytes}
	finished := make(chan error, 1)
	go func() { finished <- s.proxyServer.Serve(&trackingListener{Listener: listener, sidecar: s}) }()
	t.Cleanup(func() {
		s.stopProxy()
		select {
		case err := <-finished:
			if err != nil && !errors.Is(err, http.ErrServerClosed) {
				t.Errorf("sidecar accept loop: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("sidecar accept loop did not stop")
		}
	})
	return listener.Addr().String()
}

func dialConfiguredSidecar(t *testing.T, s *sidecar) net.Conn {
	t.Helper()
	return dialSidecar(t, serveTestSidecar(t, s))
}

type testHTTPResponse struct {
	status  int
	body    string
	refusal string
}

func getThroughSidecar(t *testing.T, address, path string, headers map[string]string) testHTTPResponse {
	t.Helper()
	conn, err := net.DialTimeout("tcp", address, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := fmt.Fprintf(conn, "GET http://upstream.test%s HTTP/1.1\r\nHost: upstream.test\r\n", path); err != nil {
		t.Fatal(err)
	}
	for name, value := range headers {
		if _, err := fmt.Fprintf(conn, "%s: %s\r\n", name, value); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := io.WriteString(conn, "\r\n"); err != nil {
		t.Fatal(err)
	}
	response, err := http.ReadResponse(bufio.NewReader(conn), &http.Request{Method: http.MethodGet})
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if err := response.Body.Close(); err != nil {
		t.Fatal(err)
	}
	return testHTTPResponse{status: response.StatusCode, body: string(body), refusal: response.Header.Get(refusedHeader)}
}

func dialSidecar(t *testing.T, address string) net.Conn {
	t.Helper()
	conn, err := net.DialTimeout("tcp", address, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	return conn
}

// exchangeRaw writes raw bytes on one connection and reads responses until the sidecar ends it.
func exchangeRaw(t *testing.T, address, raw string) []testHTTPResponse {
	t.Helper()
	conn := dialSidecar(t, address)
	// A refused oversized head fails this write once the sidecar closes; the reads report what came back.
	_, _ = io.WriteString(conn, raw)
	reader := bufio.NewReader(conn)
	var responses []testHTTPResponse
	for {
		response, err := http.ReadResponse(reader, nil)
		if err != nil {
			return responses
		}
		body, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		responses = append(responses, testHTTPResponse{status: response.StatusCode, body: string(body), refusal: response.Header.Get(refusedHeader)})
	}
}

// drainRequests returns every request the central proxy has recorded so far.
func (p *testCentralProxy) drainRequests() []testProxyRequest {
	var requests []testProxyRequest
	for {
		select {
		case request := <-p.requests:
			requests = append(requests, request)
		default:
			return requests
		}
	}
}

func connectThroughSidecar(t *testing.T, address, host string) ([]byte, net.Conn, *bufio.Reader) {
	t.Helper()
	conn, err := net.DialTimeout("tcp", address, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := fmt.Fprintf(conn, "CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\nProxy-Connection: Keep-Alive\r\n\r\n", host, host); err != nil {
		conn.Close()
		t.Fatal(err)
	}
	reader := bufio.NewReader(conn)
	head, err := readTestHead(reader)
	if err != nil {
		conn.Close()
		t.Fatal(err)
	}
	return head, conn, reader
}

func TestSidecarPlainRequestStripsProxyAuthorization(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	got := getThroughSidecar(t, address, "/path?q=1", map[string]string{"Proxy-Authorization": "Bearer forged"})
	if got.status != http.StatusOK || got.body != "seen-peer="+central.peer {
		t.Fatalf("response = %+v", got)
	}
	seen := central.nextRequest(t)
	if seen.method != http.MethodGet || seen.target != "http://upstream.test/path?q=1" || seen.peer != central.peer {
		t.Fatalf("central request = %+v", seen)
	}
	if got := seen.headers.Values("Proxy-Authorization"); len(got) != 0 {
		t.Fatalf("Proxy-Authorization values = %q", got)
	}
}

func TestMTLSHopRemovesCallerCredentials(t *testing.T) {
	request, err := http.ReadRequest(bufio.NewReader(strings.NewReader(
		"GET http://upstream.test/path HTTP/1.1\r\nProxy-Authorization: Bearer workload-forgery\r\n" +
			"X-Agentplane-Workload-Token: forged-workload-token\r\n\r\n",
	)))
	if err != nil {
		t.Fatal(err)
	}
	rewriteRequest(request)
	if got := request.Header.Values("Proxy-Authorization"); len(got) != 0 {
		t.Fatalf("mTLS forwarded hop bearer = %q", got)
	}
	if got := request.Header.Values(untrustedWorkloadTokenHeader); len(got) != 0 {
		t.Fatalf("mTLS forwarded caller-supplied workload token = %q", got)
	}
}

func TestMTLSFailureRefusesWithoutSendingTheRequest(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runtimeURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "credential-id"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	clientCredentials := ca.IssueWithUsage(t, runtimeURI, x509.ExtKeyUsageClientAuth)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	accepted := make(chan []byte, 4)
	go func() {
		for {
			connection, err := listener.Accept()
			if err != nil {
				return
			}
			go func() {
				defer connection.Close()
				_ = connection.SetReadDeadline(time.Now().Add(5 * time.Second))
				buffer := make([]byte, 8192)
				n, err := connection.Read(buffer)
				if err == nil {
					accepted <- append([]byte(nil), buffer[:n]...)
				}
				if bytes.HasPrefix(buffer[:n], []byte("GET ")) {
					_, _ = connection.Write([]byte("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"))
				} else {
					_, _ = connection.Write([]byte("not a TLS server"))
				}
			}()
		}
	}()

	s, err := newSidecar(config{
		Proxy: proxyConfig{
			Address: listener.Addr().String(),
			Transport: tlsProxyTransportConfig{
				CAFile: ca.RootFile,
				Auth:   spiffeProxyAuth{CertFile: clientCredentials.CertFile, KeyFile: clientCredentials.KeyFile},
			},
		},
		PlatformRoutes:         &platformRoutesConfig{Routes: map[string]platformRouteConfig{}},
		WorkloadRoutes:         map[string]workloadRouteConfig{},
		ListenAddress:          "127.0.0.1:0",
		ReadinessListenAddress: "127.0.0.1:0",
	})
	if err != nil {
		t.Fatalf("configure valid client mTLS identity: %v", err)
	}
	client := dialConfiguredSidecar(t, s)
	request, err := http.NewRequest(http.MethodGet, "http://upstream.test/path", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Proxy-Authorization", "Bearer caller-forgery")
	if err := request.Write(client); err != nil {
		t.Fatal(err)
	}
	response, err := http.ReadResponse(bufio.NewReader(client), request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusBadGateway || response.Header.Get(refusedHeader) != "reason=proxy-unreachable" {
		t.Fatalf("response = %d, %s; a TLS failure must refuse", response.StatusCode, response.Header.Get(refusedHeader))
	}
	_ = client.Close()

	var proxyBytes [][]byte
	select {
	case data := <-accepted:
		proxyBytes = append(proxyBytes, data)
	case <-time.After(5 * time.Second):
		t.Fatal("proxy did not observe an mTLS handshake")
	}
	for {
		select {
		case data := <-accepted:
			proxyBytes = append(proxyBytes, data)
		default:
			for _, data := range proxyBytes {
				if bytes.HasPrefix(data, []byte("GET ")) {
					t.Fatalf("mTLS failure sent an HTTP request or bearer to the proxy: %q", data)
				}
			}
			if !bytes.HasPrefix(proxyBytes[0], []byte{0x16, 0x03}) {
				t.Fatalf("proxy hop did not start with a TLS record: %q", proxyBytes[0])
			}
			return
		}
	}
}

func TestNewSidecarFailsClosedWhenMTLSFilesAreUnavailable(t *testing.T) {
	_, err := newSidecar(config{
		Proxy: proxyConfig{
			Address: "proxy.test.invalid:8888",
			Transport: tlsProxyTransportConfig{
				CAFile: "/missing/ca",
				Auth:   spiffeProxyAuth{CertFile: "/missing/cert", KeyFile: "/missing/key"},
			},
		},
		PlatformRoutes:         &platformRoutesConfig{Routes: map[string]platformRouteConfig{}},
		WorkloadRoutes:         map[string]workloadRouteConfig{},
		ListenAddress:          "127.0.0.1:0",
		ReadinessListenAddress: "127.0.0.1:0",
	})
	if err == nil {
		t.Fatal("mTLS sidecar started without its projected identity and trust files")
	}
}

func TestSidecarUnreachableCentralProxyRefuses(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	_, address := startTestSidecarWithState(t, startTestCentralProxy(t), net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	got := getThroughSidecar(t, address, "/path", nil)
	if got.status != http.StatusBadGateway || got.refusal != "reason=proxy-unreachable" {
		t.Fatalf("response = %+v", got)
	}
}

func TestSidecarForwardsInterimResponsesAndExpectContinue(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	conn := dialSidecar(t, address)
	reader := bufio.NewReader(conn)
	if _, err := io.WriteString(conn,
		"POST http://upstream.test/continue HTTP/1.1\r\n"+
			"Host: upstream.test\r\n"+
			"Content-Length: 4\r\n"+
			"Expect: 100-continue\r\n"+
			"Connection: close\r\n"+
			forgedCredentials+"\r\n"); err != nil {
		t.Fatal(err)
	}
	var gotEarlyHints, gotContinue bool
	for !gotContinue {
		response, err := http.ReadResponse(reader, &http.Request{Method: http.MethodPost})
		if err != nil {
			t.Fatal(err)
		}
		switch response.StatusCode {
		case http.StatusEarlyHints:
			gotEarlyHints = response.Header.Get("Link") == "</style.css>; rel=preload"
		case http.StatusContinue:
			gotContinue = true
		default:
			t.Fatalf("unexpected interim response: %d %v", response.StatusCode, response.Header)
		}
		_ = response.Body.Close()
	}
	if !gotEarlyHints {
		t.Fatal("central proxy's 103 Early Hints was not forwarded")
	}
	if _, err := io.WriteString(conn, "body"); err != nil {
		t.Fatal(err)
	}
	response, err := http.ReadResponse(reader, &http.Request{Method: http.MethodPost})
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || string(body) != "seen-peer="+central.peer {
		t.Fatalf("final response = %d %q", response.StatusCode, body)
	}
	seen := central.nextRequest(t)
	if seen.body != "body" {
		t.Fatalf("central request body = %q", seen.body)
	}
	assertOnlySidecarCredentials(t, seen)
}

func TestSidecarConnectIsAuthenticatedAndPipedBothWays(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	head, conn, reader := connectThroughSidecar(t, address, "tunnel.test")
	defer conn.Close()
	if string(head) != "HTTP/1.1 200 Connection established\r\n\r\n" {
		t.Fatalf("CONNECT response = %q", head)
	}
	seen := central.nextRequest(t)
	if seen.method != http.MethodConnect || seen.target != "tunnel.test:443" || seen.peer != central.peer {
		t.Fatalf("central CONNECT = %+v", seen)
	}
	if got := seen.headers.Values("Proxy-Connection"); len(got) != 0 {
		t.Fatalf("Proxy-Connection reached central proxy: %#v", got)
	}
	for _, payload := range []byte("pingpong") {
		if _, err := conn.Write([]byte{payload}); err != nil {
			t.Fatal(err)
		}
		echo := make([]byte, 1)
		if _, err := io.ReadFull(reader, echo); err != nil {
			t.Fatal(err)
		}
		if echo[0] != payload {
			t.Fatalf("tunnel byte = %q, want %q", echo[0], payload)
		}
	}
}

func TestSidecarConnectPropagatesHalfClose(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	_, conn, reader := connectThroughSidecar(t, address, "half-close.test")
	defer conn.Close()
	central.nextRequest(t)
	if _, err := io.WriteString(conn, "request"); err != nil {
		t.Fatal(err)
	}
	if tcp, okay := conn.(*net.TCPConn); okay {
		if err := tcp.CloseWrite(); err != nil {
			t.Fatal(err)
		}
	} else {
		t.Fatalf("sidecar test connection has type %T, want *net.TCPConn", conn)
	}
	response := make([]byte, len("response-after-half-close"))
	if _, err := io.ReadFull(reader, response); err != nil {
		t.Fatal(err)
	}
	if string(response) != "response-after-half-close" {
		t.Fatalf("response after client half-close = %q", response)
	}
}

func TestSidecarShutdownClosesActiveTunnel(t *testing.T) {
	central := startTestCentralProxy(t)
	s, address := startTestSidecarWithState(t, central, central.listener.Addr().String())
	_, conn, reader := connectThroughSidecar(t, address, "tunnel.test")
	defer conn.Close()
	central.nextRequest(t)
	s.stopProxy()
	if _, err := reader.ReadByte(); err == nil {
		t.Fatal("tunnel remained open after sidecar shutdown")
	}
}

func TestSidecarConnectRefusalIsRelayedAndEndsTheConnection(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	head, conn, reader := connectThroughSidecar(t, address, "refused.test")
	defer conn.Close()
	response, err := http.ReadResponse(bufio.NewReader(strings.NewReader(string(head))), &http.Request{Method: http.MethodConnect})
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusForbidden || response.Header.Get("x-agentplane-egress") != "denied; reason=no-rule" {
		t.Fatalf("CONNECT refusal = %q", head)
	}
	if _, err := reader.ReadByte(); err != io.EOF {
		t.Fatalf("after refusal, ReadByte error = %v, want EOF", err)
	}
}

func TestSidecarMalformedRequestIsRefused(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	responses := exchangeRaw(t, address, "not http at all\r\n\r\n")
	if len(responses) != 1 || responses[0].status != http.StatusBadRequest || responses[0].refusal != "" {
		t.Fatalf("malformed request responses = %+v", responses)
	}
	if central.connections.Load() != 0 {
		t.Fatalf("central proxy saw %d connections", central.connections.Load())
	}
}

func TestSidecarOversizedHeadIsRefused(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	// net/http may return 431 or close a connection whose oversized write is still in flight.
	responses := exchangeRaw(t, address, "GET http://upstream.test/ HTTP/1.1\r\nHost: upstream.test\r\nX-Padding: "+strings.Repeat("a", 2*maxHeadBytes)+"\r\n\r\n")
	if len(responses) > 1 || (len(responses) == 1 && (responses[0].status != http.StatusRequestHeaderFieldsTooLarge || responses[0].refusal != "")) {
		t.Fatalf("oversized head responses = %+v", responses)
	}
	if central.connections.Load() != 0 {
		t.Fatalf("central proxy saw %d connections", central.connections.Load())
	}
}

func assertOnlySidecarCredentials(t *testing.T, seen testProxyRequest) {
	t.Helper()
	if got := seen.headers.Values("Proxy-Authorization"); len(got) != 0 {
		t.Errorf("%s %s Proxy-Authorization = %q, want none", seen.method, seen.target, got)
	}
	if got := seen.headers.Values(untrustedWorkloadTokenHeader); len(got) != 0 {
		t.Errorf("%s %s untrusted workload token = %q, want none", seen.method, seen.target, got)
	}
}

// forgedCredentials repeats the client's own credential headers in several spellings.
const forgedCredentials = "Proxy-Authorization: Bearer forged-1\r\n" +
	"proxy-authorization: Bearer forged-2\r\n" +
	"PROXY-AUTHORIZATION: Basic Zm9yZ2VkOg==\r\n" +
	"x-agentplane-workload-token: forged-workload-3\r\n" +
	"X-AGENTPLANE-WORKLOAD-TOKEN: forged-workload-4\r\n"

func TestSidecarRewritesEveryRequestOnAKeepAliveConnection(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	conn := dialSidecar(t, address)
	reader := bufio.NewReader(conn)
	for i := range 2 {
		if _, err := fmt.Fprintf(conn, "GET http://upstream.test/%d HTTP/1.1\r\nHost: upstream.test\r\n%s\r\n", i, forgedCredentials); err != nil {
			t.Fatal(err)
		}
		response, err := http.ReadResponse(reader, nil)
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		if string(body) != "seen-peer="+central.peer {
			t.Fatalf("response %d body = %q", i, body)
		}
		assertOnlySidecarCredentials(t, central.nextRequest(t))
	}
	if got := central.connections.Load(); got != 1 {
		t.Fatalf("central proxy saw %d connections, want one for the client's one", got)
	}
}

func TestSidecarRewritesPipelinedRequests(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	smuggled := "GET http://upstream.test/smuggled HTTP/1.1\r\nHost: upstream.test\r\n" + forgedCredentials + "\r\n"
	responses := exchangeRaw(t, address,
		fmt.Sprintf("POST http://upstream.test/one HTTP/1.1\r\nHost: upstream.test\r\nContent-Length: %d\r\n%s\r\n%s", len(smuggled), forgedCredentials, smuggled)+
			"GET http://upstream.test/two HTTP/1.1\r\nHost: upstream.test\r\n"+forgedCredentials+"\r\n"+
			"GET http://upstream.test/three HTTP/1.1\r\nHost: upstream.test\r\nConnection: close\r\n"+forgedCredentials+"\r\n")
	if len(responses) != 3 {
		t.Fatalf("responses = %+v", responses)
	}
	seen := central.drainRequests()
	var targets []string
	for _, request := range seen {
		targets = append(targets, request.target)
		assertOnlySidecarCredentials(t, request)
	}
	if strings.Join(targets, " ") != "http://upstream.test/one http://upstream.test/two http://upstream.test/three" {
		t.Fatalf("central targets = %q", targets)
	}
	if seen[0].body != smuggled {
		t.Fatalf("POST body = %q, want the request-shaped bytes kept as body", seen[0].body)
	}
}

func TestSidecarRewritesRequestsBehindConflictingFraming(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	smuggled := "GET http://upstream.test/smuggled HTTP/1.1\r\nHost: upstream.test\r\nConnection: close\r\n" + forgedCredentials + "\r\n"
	const lastChunk = "0\r\n\r\n"
	exchangeRaw(t, address, fmt.Sprintf(
		"POST http://upstream.test/framed HTTP/1.1\r\nHost: upstream.test\r\nContent-Length: %d\r\nTransfer-Encoding: chunked\r\n%s\r\n%s%s",
		len(lastChunk)+len(smuggled), forgedCredentials, lastChunk, smuggled,
	))
	seen := central.drainRequests()
	if len(seen) == 0 {
		t.Fatal("central proxy saw no request")
	}
	// Whichever framing the sidecar picks, it re-frames the body, so every request the central
	// proxy parses went through the rewrite.
	for _, request := range seen {
		assertOnlySidecarCredentials(t, request)
	}
}

func TestSidecarStripsObsFoldedCredentials(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	responses := exchangeRaw(t, address, "GET http://upstream.test/folded HTTP/1.1\r\nHost: upstream.test\r\n"+
		"X-Folded: one\r\n Proxy-Authorization: Bearer forged-1\r\n"+
		"Proxy-Authorization: Bearer forged-2\r\n continued\r\n"+
		"X-Agentplane-Workload-Token:\r\n forged-workload-3\r\n"+
		"Connection: close\r\n\r\n")
	if len(responses) != 1 || responses[0].status != http.StatusOK {
		t.Fatalf("responses = %+v", responses)
	}
	seen := central.nextRequest(t)
	assertOnlySidecarCredentials(t, seen)
	// A folded line continues the value before it; it never becomes a header of its own.
	if got := seen.headers.Get("X-Folded"); got != "one Proxy-Authorization: Bearer forged-1" {
		t.Fatalf("X-Folded = %q", got)
	}
}

func TestSidecarConnectStripsClientCredentials(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	conn := dialSidecar(t, address)
	if _, err := io.WriteString(conn, "CONNECT tunnel.test:443 HTTP/1.1\r\nHost: tunnel.test:443\r\n"+forgedCredentials+"\r\n"); err != nil {
		t.Fatal(err)
	}
	head, err := readTestHead(bufio.NewReader(conn))
	if err != nil {
		t.Fatal(err)
	}
	if string(head) != "HTTP/1.1 200 Connection established\r\n\r\n" {
		t.Fatalf("CONNECT response = %q", head)
	}
	assertOnlySidecarCredentials(t, central.nextRequest(t))
}

func TestSidecarUpgradeIsRewrittenThenPiped(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central)
	conn := dialSidecar(t, address)
	if _, err := io.WriteString(conn, "GET http://upstream.test/socket HTTP/1.1\r\nHost: upstream.test\r\nConnection: Upgrade\r\nUpgrade: "+testUpgradeProtocol+"\r\n"+forgedCredentials+"\r\n"); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(conn)
	response, err := http.ReadResponse(reader, nil)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusSwitchingProtocols || response.Header.Get("Upgrade") != testUpgradeProtocol {
		t.Fatalf("upgrade response = %d %v", response.StatusCode, response.Header)
	}
	assertOnlySidecarCredentials(t, central.nextRequest(t))
	if _, err := io.WriteString(conn, "ping"); err != nil {
		t.Fatal(err)
	}
	echo := make([]byte, 4)
	if _, err := io.ReadFull(reader, echo); err != nil {
		t.Fatal(err)
	}
	if string(echo) != "ping" {
		t.Fatalf("upgraded echo = %q", echo)
	}
}
