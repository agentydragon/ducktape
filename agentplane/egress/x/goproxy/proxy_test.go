package goproxyspike

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/auth"
	"golang.org/x/net/http2"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const fixtureHost = "upstream.test"

// This dependency controls only TokenReview. It neither pretends to be an API server
// nor claims to prove Kubernetes bound-token issuance/revocation.
type reviewer struct {
	mu       sync.Mutex
	accepted bool
	uid      string
}

func (r *reviewer) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: r.accepted && review.Spec.Token == "valid", Audiences: []string{"egress"},
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:workloads:runner", UID: "sa-1",
			Extra: map[string]authenticationv1.ExtraValue{"authentication.kubernetes.io/pod-name": {"runner-1"}, "authentication.kubernetes.io/pod-uid": {r.uid}},
		},
	}}, nil
}

type countedListener struct {
	net.Listener
	accepts atomic.Int64
	closed  chan struct{} // receives once per accepted connection that is closed
}

func (l *countedListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	l.accepts.Add(1)
	return &closeSignalConn{Conn: c, closed: l.closed}, nil
}

type closeSignalConn struct {
	net.Conn
	closed chan<- struct{}
	once   sync.Once
}

func (c *closeSignalConn) Close() error {
	c.once.Do(func() {
		select {
		case c.closed <- struct{}{}:
		default: // only tests that wait on it read the channel
		}
	})
	return c.Conn.Close()
}

type lab struct {
	snapshot    Snapshot
	mu          sync.Mutex
	reviewer    *reviewer
	options     Options
	proxy       *httptest.Server
	roots       *x509.CertPool
	upstreamCA  tls.Certificate
	resolutions atomic.Int64
}

func newLab(t *testing.T) *lab {
	t.Helper()
	intercept, interceptRoot := makeCA(t, "interception")
	upstream, upstreamRoot := makeCA(t, "upstream")
	reviews := &reviewer{accepted: true, uid: "pod-1"}
	resolver, err := auth.NewResolver(reviews, "egress", []string{"workloads"})
	if err != nil {
		t.Fatal(err)
	}
	l := &lab{
		reviewer: reviews, roots: interceptRoot, upstreamCA: upstream,
		snapshot: Snapshot{
			FreshUntil: time.Now().Add(time.Hour), Credentials: map[string]HeaderCredential{"key": {Header: "x-api-key", Value: "real-secret"}},
			Sets:     []policyapi.PolicySet{{ObjectMeta: metav1.ObjectMeta{Name: "egress", Namespace: "rules"}, Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: "egress", Hosts: []string{fixtureHost}, Paths: []string{"/allowed", "/stream", "/sse", "/ws", "/cancel"}, CredentialRef: &policyapi.CredentialRef{Name: "key"}}}}}},
			Bindings: []policyapi.PolicyBinding{{ObjectMeta: metav1.ObjectMeta{Name: "runner", Namespace: "rules"}, Spec: policyapi.PolicyBindingSpec{Subject: policyapi.ServiceAccountRef{Namespace: "workloads", Name: "runner"}, PolicySets: []string{"egress"}}}},
		},
	}
	l.options = Options{
		CA: intercept, Roots: upstreamRoot, Auth: resolver, ReadSnapshot: func() (Snapshot, error) { l.mu.Lock(); defer l.mu.Unlock(); return l.snapshot, nil },
		Resolve: func(context.Context, string, string) ([]netip.Addr, error) {
			l.resolutions.Add(1)
			return []netip.Addr{netip.MustParseAddr("127.0.0.1")}, nil
		},
		Exempt: []netip.Prefix{netip.MustParsePrefix("127.0.0.1/32")},
	}
	l.start(t)
	return l
}

func (l *lab) start(t *testing.T) {
	t.Helper()
	handler, err := New(l.options)
	if err != nil {
		t.Fatal(err)
	}
	l.proxy = httptest.NewServer(handler)
	t.Cleanup(l.proxy.Close)
}

func (l *lab) upstream(t *testing.T, name string, tlsEnabled bool, handler http.Handler) (*httptest.Server, *countedListener) {
	t.Helper()
	server := httptest.NewUnstartedServer(handler)
	counted := &countedListener{Listener: server.Listener, closed: make(chan struct{}, 16)}
	server.Listener = counted
	server.EnableHTTP2 = true
	if tlsEnabled {
		server.TLS = &tls.Config{Certificates: []tls.Certificate{issue(t, l.upstreamCA, name)}}
		server.StartTLS()
	} else {
		server.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			t.Errorf("h2c bypassed ServeConn: %s", r.Proto)
			http.Error(w, "bad protocol", 500)
		})
		// h2c uses the same accepted TCP boundary as TLS, without a separate fake server.
		go func() {
			for {
				connection, err := counted.Accept()
				if err != nil {
					return
				}
				go (&http2.Server{}).ServeConn(connection, &http2.ServeConnOpts{Handler: handler})
			}
		}()
	}
	t.Cleanup(server.Close)
	return server, counted
}

func (l *lab) tunnel(t *testing.T, port string, tlsEnabled bool) (net.Conn, *http2.ClientConn) {
	t.Helper()
	connection, err := net.Dial("tcp", l.proxy.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	if _, err := fmt.Fprintf(connection, "CONNECT %s:%s HTTP/1.1\r\nHost: %s:%s\r\nProxy-Authorization: Bearer valid\r\n\r\n", fixtureHost, port, fixtureHost, port); err != nil {
		t.Fatal(err)
	}
	response, err := http.ReadResponse(bufio.NewReader(connection), &http.Request{Method: "CONNECT"})
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != 200 {
		t.Fatalf("CONNECT: %s", response.Status)
	}
	if tlsEnabled {
		tlsConnection := tls.Client(connection, &tls.Config{RootCAs: l.roots, ServerName: fixtureHost, NextProtos: []string{"h2"}})
		if err := tlsConnection.Handshake(); err != nil {
			t.Fatal(err)
		}
		connection = tlsConnection
	}
	h2, err := (&http2.Transport{}).NewClientConn(connection)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { h2.Close() })
	return connection, h2
}

func request(ctx context.Context, port, path string, tlsEnabled bool, body io.Reader) *http.Request {
	scheme := "http"
	if tlsEnabled {
		scheme = "https"
	}
	req, _ := http.NewRequestWithContext(ctx, "POST", scheme+"://"+fixtureHost+":"+port+path, body)
	req.Header.Set("x-api-key", "agentplane-credential-key")
	req.Header.Set("Content-Type", "application/grpc")
	return req
}

func portOf(server *httptest.Server) string {
	return fmt.Sprint(server.Listener.Addr().(*net.TCPAddr).Port)
}

func bounded(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func TestDeniedInnerRequestMakesZeroTCPConnections(t *testing.T) {
	l := newLab(t)
	upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Error("denial reached upstream") }))
	_, client := l.tunnel(t, portOf(upstream), true)
	response, err := client.RoundTrip(request(bounded(t), portOf(upstream), "/denied", true, nil))
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != 403 || counted.accepts.Load() != 0 || l.resolutions.Load() != 0 {
		t.Fatalf("denial: status=%d TCP=%d DNS=%d", response.StatusCode, counted.accepts.Load(), l.resolutions.Load())
	}
}

func TestInteractiveH2AndH2CTrailersCredentialAndCancellation(t *testing.T) {
	for _, encrypted := range []bool{true, false} {
		t.Run(fmt.Sprint("tls=", encrypted), func(t *testing.T) {
			l := newLab(t)
			cancelled := make(chan struct{})
			upstream, counted := l.upstream(t, fixtureHost, encrypted, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.ProtoMajor != 2 || r.Header.Get("x-api-key") != "real-secret" || r.Header.Get("Proxy-Authorization") != "" {
					t.Errorf("upstream context: protocol=%s headers=%v", r.Proto, r.Header)
				}
				w.Header().Set("Content-Type", "application/grpc")
				w.Header().Set("Trailer", "Grpc-Status, X-Test-Trailer")
				w.WriteHeader(200)
				w.(http.Flusher).Flush()
				if r.URL.Path == "/cancel" {
					<-r.Context().Done()
					close(cancelled)
					return
				}
				frame := make([]byte, 5)
				if _, err := io.ReadFull(r.Body, frame); err != nil {
					t.Errorf("read first frame: %v", err)
					return
				}
				w.Write(frame)
				w.(http.Flusher).Flush()
				if _, err := io.ReadFull(r.Body, frame); err != io.EOF {
					t.Errorf("expected EOF: %v", err)
				}
				w.Header().Set("Grpc-Status", "0")
				w.Header().Set("X-Test-Trailer", "ok")
			}))
			_, client := l.tunnel(t, portOf(upstream), encrypted)
			reader, writer := io.Pipe()
			defer writer.Close()
			result := make(chan *http.Response, 1)
			failures := make(chan error, 1)
			go func() {
				response, err := client.RoundTrip(request(bounded(t), portOf(upstream), "/stream", encrypted, reader))
				if err != nil {
					failures <- err
					return
				}
				result <- response
			}()
			if _, err := writer.Write([]byte{0, 0, 0, 0, 0}); err != nil {
				t.Fatal(err)
			}
			var response *http.Response
			select {
			case response = <-result:
			case err := <-failures:
				t.Fatal(err)
			case <-time.After(5 * time.Second):
				t.Fatal("response headers waited for request EOF")
			}
			frame := make([]byte, 5)
			if _, err := io.ReadFull(response.Body, frame); err != nil {
				t.Fatal(err)
			}
			// The read above completes BEFORE request EOF: this is the interactive contract.
			writer.Close()
			if _, err := io.ReadAll(response.Body); err != nil {
				t.Fatal(err)
			}
			response.Body.Close()
			if response.Trailer.Get("Grpc-Status") != "0" || response.Trailer.Get("X-Test-Trailer") != "ok" {
				t.Fatalf("trailers: %v", response.Trailer)
			}
			ctx, cancel := context.WithCancel(bounded(t))
			cancellation, err := client.RoundTrip(request(ctx, portOf(upstream), "/cancel", encrypted, nil))
			if err != nil {
				t.Fatal(err)
			}
			cancel()
			cancellation.Body.Close()
			select {
			case <-cancelled:
			case <-time.After(5 * time.Second):
				t.Fatal("client cancellation never reached upstream")
			}
			if counted.accepts.Load() != 2 || l.resolutions.Load() != 2 {
				t.Fatalf("TCP=%d DNS=%d", counted.accepts.Load(), l.resolutions.Load())
			}
		})
	}
}

func TestUpstreamConnectionIsReleasedAfterItsOnlyResponse(t *testing.T) {
	for _, encrypted := range []bool{true, false} {
		t.Run(fmt.Sprint("tls=", encrypted), func(t *testing.T) {
			l := newLab(t)
			upstream, counted := l.upstream(t, fixtureHost, encrypted, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("done")) }))
			_, client := l.tunnel(t, portOf(upstream), encrypted)
			response, err := client.RoundTrip(request(bounded(t), portOf(upstream), "/allowed", encrypted, nil))
			if err != nil {
				t.Fatal(err)
			}
			io.Copy(io.Discard, response.Body)
			response.Body.Close()
			select {
			case <-counted.closed:
			case <-time.After(5 * time.Second):
				t.Fatal("upstream connection stayed open after its only response")
			}
		})
	}
}

func TestRejectsUntrustedCAAndWrongHostname(t *testing.T) {
	for _, name := range []string{"untrusted", "wrong-host", "interception-root"} {
		t.Run(name, func(t *testing.T) {
			l := newLab(t)
			host := fixtureHost
			if name == "untrusted" {
				l.upstreamCA, _ = makeCA(t, "untrusted")
			} else if name == "interception-root" {
				l.upstreamCA = l.options.CA
			} else {
				host = "other.test"
			}
			var requests atomic.Int64
			upstream, counted := l.upstream(t, host, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { requests.Add(1); w.Write([]byte("unexpected")) }))
			_, client := l.tunnel(t, portOf(upstream), true)
			response, err := client.RoundTrip(request(bounded(t), portOf(upstream), "/allowed", true, nil))
			if err != nil {
				t.Fatal(err)
			}
			response.Body.Close()
			if response.StatusCode != 502 || requests.Load() != 0 || counted.accepts.Load() != 1 {
				t.Fatalf("certificate check: status=%d requests=%d TCP=%d", response.StatusCode, requests.Load(), counted.accepts.Load())
			}
		})
	}
}

func TestPinnedDialDoesNotResolveAgain(t *testing.T) {
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	l := newLab(t)
	upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("pinned")) }))
	// upstream.test has no system DNS entry. A hostname dial instead of the literal
	// approved IP cannot reach this listener; a second lookup returns a forbidden address.
	var calls atomic.Int64
	l.options.Resolve = func(context.Context, string, string) ([]netip.Addr, error) {
		if calls.Add(1) > 1 {
			return []netip.Addr{netip.MustParseAddr("169.254.169.254")}, nil
		}
		return []netip.Addr{netip.MustParseAddr("127.0.0.1")}, nil
	}
	l.start(t)
	_, client := l.tunnel(t, portOf(upstream), true)
	response, err := client.RoundTrip(request(bounded(t), portOf(upstream), "/allowed", true, nil))
	if err != nil {
		t.Fatal(err)
	}
	data, err := io.ReadAll(response.Body)
	response.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "pinned" || calls.Load() != 1 || counted.accepts.Load() != 1 {
		t.Fatalf("dial: body=%q DNS=%d TCP=%d", data, calls.Load(), counted.accepts.Load())
	}
}

func TestRevocationDuringDNSAndMixedForbiddenAnswersNeverDial(t *testing.T) {
	for _, scenario := range []string{"revoked-during-dns", "mixed-answers"} {
		t.Run(scenario, func(t *testing.T) {
			l := newLab(t)
			upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Error("denied DNS admission reached upstream") }))
			l.options.Resolve = func(context.Context, string, string) ([]netip.Addr, error) {
				if scenario == "revoked-during-dns" {
					l.mu.Lock()
					l.snapshot.Bindings = nil
					l.mu.Unlock()
				}
				addresses := []netip.Addr{netip.MustParseAddr("127.0.0.1")}
				if scenario == "mixed-answers" {
					addresses = append(addresses, netip.MustParseAddr("::ffff:169.254.169.254"))
				}
				return addresses, nil
			}
			l.start(t)
			_, connection := l.tunnel(t, portOf(upstream), true)
			response, err := connection.RoundTrip(request(bounded(t), portOf(upstream), "/allowed", true, nil))
			if err != nil {
				t.Fatal(err)
			}
			response.Body.Close()
			if response.StatusCode != 403 || counted.accepts.Load() != 0 {
				t.Fatalf("denied after DNS: status=%d TCP=%d", response.StatusCode, counted.accepts.Load())
			}
		})
	}
}

func TestExistingConnectionRevocationStalenessAndAuthCleanup(t *testing.T) {
	for _, change := range []string{"binding", "stale", "invalid-replacement", "changed-principal"} {
		t.Run(change, func(t *testing.T) {
			l := newLab(t)
			upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("allowed")) }))
			_, client := l.tunnel(t, portOf(upstream), true)
			send := func(header string) int {
				req := request(bounded(t), portOf(upstream), "/allowed", true, nil)
				if header != "" {
					req.Header.Set("Proxy-Authorization", header)
				}
				response, err := client.RoundTrip(req)
				if err != nil {
					t.Fatal(err)
				}
				io.Copy(io.Discard, response.Body)
				response.Body.Close()
				return response.StatusCode
			}
			if got := send(""); got != 200 {
				t.Fatalf("initial request=%d", got)
			}
			switch change {
			case "binding":
				l.mu.Lock()
				l.snapshot.Bindings = nil
				l.mu.Unlock()
			case "stale":
				l.mu.Lock()
				l.snapshot.FreshUntil = time.Now().Add(-time.Second)
				l.mu.Unlock()
			case "changed-principal":
				l.reviewer.mu.Lock()
				l.reviewer.uid = "replacement-pod"
				l.reviewer.mu.Unlock()
			}
			header := ""
			if change == "invalid-replacement" {
				header = "invalid"
			}
			if got := send(header); got != 403 {
				t.Fatalf("revoked request=%d", got)
			}
			if got := send(""); got != 403 {
				t.Fatalf("later request recovered stale auth/policy=%d", got)
			}
			if counted.accepts.Load() != 1 {
				t.Fatalf("denial opened upstream TCP: %d", counted.accepts.Load())
			}
		})
	}
}

func TestIncrementalSSEAndWebSocket(t *testing.T) {
	l := newLab(t)
	release := make(chan struct{})
	defer close(release)
	upstream, _ := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sse" {
			w.Header().Set("Content-Type", "text/event-stream")
			w.Write([]byte("data: first\n\n"))
			w.(http.Flusher).Flush()
			<-release
			return
		}
		connection, buffer, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		defer connection.Close()
		// RFC6455 fixed example key/accept. Echo a small unmasked server frame.
		buffer.WriteString("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
		buffer.Flush()
		packet := make([]byte, 8)
		if _, err := io.ReadFull(buffer, packet); err != nil {
			t.Error(err)
			return
		}
		connection.Write([]byte{0x81, 2, 'o', 'k'})
	}))
	_, h2 := l.tunnel(t, portOf(upstream), true)
	response, err := h2.RoundTrip(request(bounded(t), portOf(upstream), "/sse", true, nil))
	if err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(response.Body).ReadString('\n')
	response.Body.Close()
	if err != nil || line != "data: first\n" {
		t.Fatalf("incremental SSE %q %v", line, err)
	}
	// WebSocket upgrades require HTTP/1 on both legs; HTTP/2 extended CONNECT is
	// a different protocol and intentionally outside this spike.
	proxyURL, _ := url.Parse(l.proxy.URL)
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL), TLSClientConfig: &tls.Config{RootCAs: l.roots}, ProxyConnectHeader: http.Header{"Proxy-Authorization": {"Bearer valid"}}}}
	req, _ := http.NewRequestWithContext(bounded(t), "GET", "https://"+fixtureHost+":"+portOf(upstream)+"/ws", nil)
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", "websocket")
	req.Header.Set("Sec-WebSocket-Version", "13")
	req.Header.Set("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
	upgraded, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer upgraded.Body.Close()
	if upgraded.StatusCode != 101 {
		t.Fatalf("websocket: %s", upgraded.Status)
	}
	socket, ok := upgraded.Body.(io.ReadWriteCloser)
	if !ok {
		t.Fatal("upgrade body not duplex")
	}
	socket.Write([]byte{0x81, 0x82, 1, 2, 3, 4, 'h' ^ 1, 'i' ^ 2})
	reply := make([]byte, 4)
	if _, err := io.ReadFull(socket, reply); err != nil || string(reply[2:]) != "ok" {
		t.Fatalf("websocket frame %q %v", reply, err)
	}
}

func makeCA(t *testing.T, name string) (tls.Certificate, *x509.CertPool) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: name}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	raw, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := x509.ParseCertificate(raw)
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(parsed)
	return tls.Certificate{Certificate: [][]byte{raw}, PrivateKey: key, Leaf: parsed}, pool
}

func issue(t *testing.T, ca tls.Certificate, host string) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(time.Now().UnixNano()), DNSNames: []string{host}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, KeyUsage: x509.KeyUsageDigitalSignature}
	raw, err := x509.CreateCertificate(rand.Reader, template, ca.Leaf, &key.PublicKey, ca.PrivateKey)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{raw}, PrivateKey: key}
}
