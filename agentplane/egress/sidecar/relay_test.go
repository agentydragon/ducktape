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

	"github.com/agentydragon/ducktape/agentplane/egress/sidecar/configpb"
	"google.golang.org/protobuf/proto"
)

const testAudience = "https://kubernetes.test.invalid"

type testProxyRequest struct {
	method  string
	target  string
	headers http.Header
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
	request, err := http.ReadRequest(reader)
	if err != nil {
		return
	}
	observed := testProxyRequest{
		method:  request.Method,
		target:  request.RequestURI,
		headers: request.Header.Clone(),
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
	body := []byte("seen-token=" + observed.token())
	_, _ = fmt.Fprintf(conn, "HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n", len(body))
	_, _ = conn.Write(body)
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
		config: &configpb.Config{
			ProxyHost:          "127.0.0.1",
			ProxyPort:          proto.Int32(int32(proxyPort)),
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
	if got := seen.headers.Get("Connection"); got != "close" {
		t.Fatalf("central Connection = %q, want close", got)
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

func TestSidecarConnectRefusalIsRelayedVerbatim(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central.port(), writeTestToken(t, "token-1"), nil)
	head, conn, reader := connectThroughSidecar(t, address, "refused.test")
	defer conn.Close()
	const want = "HTTP/1.1 403 Forbidden\r\nx-agentplane-egress: denied; reason=no-rule\r\n\r\n"
	if string(head) != want {
		t.Fatalf("CONNECT refusal = %q, want %q", head, want)
	}
	if _, err := reader.ReadByte(); err != io.EOF {
		t.Fatalf("after refusal, ReadByte error = %v, want EOF", err)
	}
}

func TestSidecarMalformedRequestIsRefused(t *testing.T) {
	central := startTestCentralProxy(t)
	address := startTestSidecar(t, central.port(), writeTestToken(t, "token-1"), nil)
	conn, err := net.DialTimeout("tcp", address, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := io.WriteString(conn, "not http at all\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	head, err := readTestHead(bufio.NewReader(conn))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(string(head), "HTTP/1.1 400 ") || !strings.Contains(string(head), refusedHeader+": reason=bad-request") {
		t.Fatalf("malformed request response = %q", head)
	}
	if central.connections.Load() != 0 {
		t.Fatalf("central proxy saw %d connections", central.connections.Load())
	}
}

func audienceSidecar(t *testing.T, central *testCentralProxy) (string, string) {
	t.Helper()
	tokenFile := writeTestToken(t, "token-1")
	audienceFile := writeTestToken(t, "api-server-token-1\n")
	return startTestSidecar(t, central.port(), tokenFile, map[string]string{testAudience: audienceFile}), audienceFile
}

func TestSidecarOffersConfiguredAudienceBesideHopToken(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _ := audienceSidecar(t, central)
	getThroughSidecar(t, address, "/path", nil)
	seen := central.nextRequest(t)
	if seen.token() != "token-1" || strings.Join(seen.projectedTokens(), ",") != testAudience+" api-server-token-1" {
		t.Fatalf("central tokens = hop %q, projected %#v", seen.token(), seen.projectedTokens())
	}
}

func TestSidecarNeverForwardsClientProjectedHeader(t *testing.T) {
	central := startTestCentralProxy(t)
	address, _ := audienceSidecar(t, central)
	getThroughSidecar(t, address, "/path", map[string]string{projectedTokenHeader: testAudience + " stolen-token-of-another-pod"})
	seen := central.nextRequest(t)
	if got := strings.Join(seen.projectedTokens(), ","); got != testAudience+" api-server-token-1" {
		t.Fatalf("projected tokens = %q", got)
	}
}

func TestSidecarRereadsProjectedTokenBetweenRequests(t *testing.T) {
	central := startTestCentralProxy(t)
	address, audienceFile := audienceSidecar(t, central)
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
	address, _ := audienceSidecar(t, central)
	_, conn, _ := connectThroughSidecar(t, address, "tunnel.test")
	defer conn.Close()
	if tokens := central.nextRequest(t).projectedTokens(); strings.Join(tokens, ",") != testAudience+" api-server-token-1" {
		t.Fatalf("CONNECT projected tokens = %#v", tokens)
	}
}
