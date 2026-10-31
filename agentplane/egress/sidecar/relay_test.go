package main

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testAudience        = "https://kubernetes.test.invalid"
	testUpgradeProtocol = "echo.test"
)

type testProxyRequest struct {
	method  string
	target  string
	headers http.Header
	body    string
}

func (r testProxyRequest) token() string {
	for _, value := range r.headers.Values("Proxy-Authorization") {
		scheme, token, okay := strings.Cut(value, " ")
		if okay && scheme == "Bearer" {
			return token
		}
	}
	return ""
}

func (r testProxyRequest) projectedTokens() []string {
	return r.headers.Values(projectedTokenHeader)
}

type testCentralProxy struct {
	listener    net.Listener
	requests    chan testProxyRequest
	connections atomic.Int64
	acceptDone  chan struct{}

	mu       sync.Mutex
	active   map[net.Conn]struct{}
	handlers sync.WaitGroup
}

func startTestCentralProxy(t *testing.T) *testCentralProxy {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	proxy := &testCentralProxy{
		listener:   listener,
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

func (p *testCentralProxy) port() int { return p.listener.Addr().(*net.TCPAddr).Port }

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
	reader := bufio.NewReader(conn)
	for {
		request, err := http.ReadRequest(reader)
		if err != nil {
			return
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
		response := "seen-token=" + observed.token()
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

func startTestSidecar(t *testing.T, proxyPort int, tokenFile string, audiences map[string]string) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := &sidecar{
		config: config{
			ProxyHost:          "127.0.0.1",
			ProxyPort:          proxyPort,
			TokenFile:          tokenFile,
			AudienceTokenFiles: audiences,
		},
		proxyListener: listener,
	}
	finished := make(chan error, 1)
	go func() { finished <- s.acceptLoop() }()
	t.Cleanup(func() {
		s.stopProxy()
		select {
		case err := <-finished:
			if err != nil {
				t.Errorf("sidecar accept loop: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("sidecar accept loop did not stop")
		}
	})
	return listener.Addr().String()
}

func writeTestToken(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
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

func TestSidecarPlainRequestReplacesProxyAuthorization(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central.port(), writeTestToken(t, "token-1\n"), nil)
	got := getThroughSidecar(t, address, "/path?q=1", map[string]string{"Proxy-Authorization": "Bearer forged"})
	if got.status != http.StatusOK || got.body != "seen-token=token-1" {
		t.Fatalf("response = %+v", got)
	}
	seen := central.nextRequest(t)
	if seen.method != http.MethodGet || seen.target != "http://upstream.test/path?q=1" || seen.token() != "token-1" {
		t.Fatalf("central request = %+v", seen)
	}
	proxyAuthorization := seen.headers.Values("Proxy-Authorization")
	if len(proxyAuthorization) != 1 || proxyAuthorization[0] != "Bearer token-1" {
		t.Fatalf("Proxy-Authorization values = %q", proxyAuthorization)
	}
}

func TestSidecarTokenIsRereadOnEveryRequest(t *testing.T) {
	central := startTestCentralProxy(t)
	tokenFile := writeTestToken(t, "token-1\n")
	address := startTestSidecar(t, central.port(), tokenFile, nil)
	getThroughSidecar(t, address, "/one", nil)
	if err := os.WriteFile(tokenFile, []byte("token-2"), 0o600); err != nil {
		t.Fatal(err)
	}
	getThroughSidecar(t, address, "/two", nil)
	if first, second := central.nextRequest(t).token(), central.nextRequest(t).token(); first != "token-1" || second != "token-2" {
		t.Fatalf("central tokens = %q, %q", first, second)
	}
}

func TestSidecarMissingTokenRefusesWithoutForwarding(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central.port(), filepath.Join(t.TempDir(), "absent"), nil)
	got := getThroughSidecar(t, address, "/path", nil)
	if got.status != http.StatusServiceUnavailable || got.refusal != "reason=token-unavailable" {
		t.Fatalf("response = %+v", got)
	}
	if central.connections.Load() != 0 {
		t.Fatalf("central proxy saw %d connections", central.connections.Load())
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
	address := startTestSidecar(t, port, writeTestToken(t, "token-1"), nil)
	got := getThroughSidecar(t, address, "/path", nil)
	if got.status != http.StatusBadGateway || got.refusal != "reason=proxy-unreachable" {
		t.Fatalf("response = %+v", got)
	}
}

func TestSidecarConnectIsAuthenticatedAndPipedBothWays(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central.port(), writeTestToken(t, "token-1"), nil)
	head, conn, reader := connectThroughSidecar(t, address, "tunnel.test")
	defer conn.Close()
	if string(head) != "HTTP/1.1 200 Connection established\r\n\r\n" {
		t.Fatalf("CONNECT response = %q", head)
	}
	seen := central.nextRequest(t)
	if seen.method != http.MethodConnect || seen.target != "tunnel.test:443" || seen.token() != "token-1" {
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

func TestSidecarConnectRefusalIsRelayedAndEndsTheConnection(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central.port(), writeTestToken(t, "token-1"), nil)
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
	address := startTestSidecar(t, central.port(), writeTestToken(t, "token-1"), nil)
	responses := exchangeRaw(t, address, "not http at all\r\n\r\n")
	if len(responses) != 1 || responses[0].status != http.StatusBadRequest || responses[0].refusal != "reason=bad-request" {
		t.Fatalf("malformed request responses = %+v", responses)
	}
	if central.connections.Load() != 0 {
		t.Fatalf("central proxy saw %d connections", central.connections.Load())
	}
}

func TestSidecarOversizedHeadIsRefused(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central.port(), writeTestToken(t, "token-1"), nil)
	// The sidecar stops reading at the limit, so its close may reset the connection before the
	// refusal is read; either way nothing reaches the central proxy.
	responses := exchangeRaw(t, address, "GET http://upstream.test/ HTTP/1.1\r\nHost: upstream.test\r\nX-Padding: "+strings.Repeat("a", 2*maxHeadBytes)+"\r\n\r\n")
	if len(responses) > 1 || (len(responses) == 1 && (responses[0].status != http.StatusBadRequest || responses[0].refusal != "reason=bad-request")) {
		t.Fatalf("oversized head responses = %+v", responses)
	}
	if central.connections.Load() != 0 {
		t.Fatalf("central proxy saw %d connections", central.connections.Load())
	}
}

// audienceSidecar starts a sidecar with one projected audience and returns its address, hop token
// file and audience token file.
func audienceSidecar(t *testing.T, central *testCentralProxy) (string, string, string) {
	t.Helper()
	tokenFile := writeTestToken(t, "token-1")
	audienceFile := writeTestToken(t, "api-server-token-1\n")
	return startTestSidecar(t, central.port(), tokenFile, map[string]string{testAudience: audienceFile}), tokenFile, audienceFile
}

func TestSidecarOffersConfiguredAudienceBesideHopToken(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _, _ := audienceSidecar(t, central)
	getThroughSidecar(t, address, "/path", nil)
	seen := central.nextRequest(t)
	if seen.token() != "token-1" || strings.Join(seen.projectedTokens(), ",") != testAudience+" api-server-token-1" {
		t.Fatalf("central tokens = hop %q, projected %#v", seen.token(), seen.projectedTokens())
	}
}

func TestSidecarNeverForwardsClientProjectedHeader(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _, _ := audienceSidecar(t, central)
	getThroughSidecar(t, address, "/path", map[string]string{projectedTokenHeader: testAudience + " stolen-token-of-another-pod"})
	seen := central.nextRequest(t)
	if got := strings.Join(seen.projectedTokens(), ","); got != testAudience+" api-server-token-1" {
		t.Fatalf("projected tokens = %q", got)
	}
}

func TestSidecarRereadsProjectedTokenBetweenRequests(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _, audienceFile := audienceSidecar(t, central)
	getThroughSidecar(t, address, "/one", nil)
	if err := os.WriteFile(audienceFile, []byte("api-server-token-2"), 0o600); err != nil {
		t.Fatal(err)
	}
	getThroughSidecar(t, address, "/two", nil)
	first := central.nextRequest(t).projectedTokens()
	second := central.nextRequest(t).projectedTokens()
	if strings.Join(first, ",") != testAudience+" api-server-token-1" || strings.Join(second, ",") != testAudience+" api-server-token-2" {
		t.Fatalf("projected tokens = %#v then %#v", first, second)
	}
}

func TestSidecarOmitsUnavailableAudienceToken(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central.port(), writeTestToken(t, "token-1"), map[string]string{
		testAudience: filepath.Join(t.TempDir(), "absent"),
	})
	got := getThroughSidecar(t, address, "/path", nil)
	if got.status != http.StatusOK {
		t.Fatalf("response = %+v", got)
	}
	if tokens := central.nextRequest(t).projectedTokens(); len(tokens) != 0 {
		t.Fatalf("unavailable audience was forwarded: %#v", tokens)
	}
}

func TestSidecarConnectForwardsProjectedToken(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _, _ := audienceSidecar(t, central)
	_, conn, _ := connectThroughSidecar(t, address, "tunnel.test")
	defer conn.Close()
	if tokens := central.nextRequest(t).projectedTokens(); strings.Join(tokens, ",") != testAudience+" api-server-token-1" {
		t.Fatalf("CONNECT projected tokens = %#v", tokens)
	}
}

func assertOnlySidecarCredentials(t *testing.T, seen testProxyRequest, token string) {
	t.Helper()
	if got := seen.headers.Values("Proxy-Authorization"); len(got) != 1 || got[0] != "Bearer "+token {
		t.Errorf("%s %s Proxy-Authorization = %q, want only the sidecar's", seen.method, seen.target, got)
	}
	if got := seen.projectedTokens(); len(got) != 1 || got[0] != testAudience+" api-server-token-1" {
		t.Errorf("%s %s projected tokens = %q, want only the sidecar's", seen.method, seen.target, got)
	}
}

// forgedCredentials repeats the client's own credential headers in several spellings.
const forgedCredentials = "Proxy-Authorization: Bearer forged-1\r\n" +
	"proxy-authorization: Bearer forged-2\r\n" +
	"PROXY-AUTHORIZATION: Basic Zm9yZ2VkOg==\r\n" +
	"x-agentplane-workload-token: " + testAudience + " forged-3\r\n" +
	"X-AGENTPLANE-WORKLOAD-TOKEN: " + testAudience + " forged-4\r\n"

func TestSidecarRewritesEveryRequestOnAKeepAliveConnection(t *testing.T) {
	central := startTestCentralProxy(t)
	address, tokenFile, _ := audienceSidecar(t, central)
	conn := dialSidecar(t, address)
	reader := bufio.NewReader(conn)
	for i, token := range []string{"token-1", "token-2"} {
		if err := os.WriteFile(tokenFile, []byte(token), 0o600); err != nil {
			t.Fatal(err)
		}
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
		if string(body) != "seen-token="+token {
			t.Fatalf("response %d body = %q", i, body)
		}
		assertOnlySidecarCredentials(t, central.nextRequest(t), token)
	}
	if got := central.connections.Load(); got != 1 {
		t.Fatalf("central proxy saw %d connections, want one for the client's one", got)
	}
}

func TestSidecarRewritesPipelinedRequests(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _, _ := audienceSidecar(t, central)
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
		assertOnlySidecarCredentials(t, request, "token-1")
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
	address, _, _ := audienceSidecar(t, central)
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
		assertOnlySidecarCredentials(t, request, "token-1")
	}
}

func TestSidecarStripsObsFoldedCredentials(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _, _ := audienceSidecar(t, central)
	responses := exchangeRaw(t, address, "GET http://upstream.test/folded HTTP/1.1\r\nHost: upstream.test\r\n"+
		"X-Folded: one\r\n Proxy-Authorization: Bearer forged-1\r\n"+
		"Proxy-Authorization: Bearer forged-2\r\n continued\r\n"+
		"X-Agentplane-Workload-Token:\r\n "+testAudience+" forged-3\r\n"+
		"Connection: close\r\n\r\n")
	if len(responses) != 1 || responses[0].status != http.StatusOK {
		t.Fatalf("responses = %+v", responses)
	}
	seen := central.nextRequest(t)
	assertOnlySidecarCredentials(t, seen, "token-1")
	// A folded line continues the value before it; it never becomes a header of its own.
	if got := seen.headers.Get("X-Folded"); got != "one Proxy-Authorization: Bearer forged-1" {
		t.Fatalf("X-Folded = %q", got)
	}
}

func TestSidecarConnectStripsClientCredentials(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _, _ := audienceSidecar(t, central)
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
	assertOnlySidecarCredentials(t, central.nextRequest(t), "token-1")
}

func TestSidecarUpgradeIsRewrittenThenPiped(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _, _ := audienceSidecar(t, central)
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
	assertOnlySidecarCredentials(t, central.nextRequest(t), "token-1")
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
