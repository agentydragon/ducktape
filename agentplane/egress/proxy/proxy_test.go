package proxy

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	"github.com/agentydragon/ducktape/agentplane/egress/upstream"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	"github.com/google/uuid"
	"golang.org/x/net/http2"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const (
	fixtureHost       = "upstream.test"
	hopAudience       = "test-egress"
	projectedAudience = "https://kubernetes.test.invalid"
)

var (
	kube           *kubeapiserver.Server
	runner         = auth.Account{Namespace: "test-workloads"}
	runnerPod      *corev1.Pod
	hopToken       string // Retained only to prove Proxy-Authorization cannot authenticate.
	projectedToken string
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error {
		kube = server
		return nil
	}))
}

// runnerToken is a new Pod running as runner and a token for audience bound to it.
func runnerToken(t *testing.T, audience string) (*corev1.Pod, string) {
	t.Helper()
	pod, err := kube.Pod(t.Context(), runner)
	if err != nil {
		t.Fatal(err)
	}
	token, err := kube.PodToken(t.Context(), pod, audience)
	if err != nil {
		t.Fatal(err)
	}
	return pod, token
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

// records collects the decisions the proxy reports.
type records struct {
	mu   sync.Mutex
	list []decision.Record
}

func (r *records) record(record decision.Record) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.list = append(r.list, record)
}

func (r *records) all() []decision.Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]decision.Record(nil), r.list...)
}

type lab struct {
	index          *policy.Index
	records        *records
	options        Options
	proxy          *Proxy
	address        string
	roots          *x509.CertPool
	proxyClientTLS *tls.Config
	workload       workloadauthtesting.Workload
	upstreamCA     tls.Certificate
	// lookup answers the proxy's DNS lookups; replace it before sending traffic.
	lookup      func(host string) []netip.Addr
	resolutions atomic.Int64
	// skew moves the resolver's clock forward, to expire the pins it holds.
	skew atomic.Int64
}

var keyCredential = resources.Credential{
	Name: "key", Source: resources.SecretKey{Name: "keys", Key: "key"},
	Targets: []resources.Target{resources.WholeValue{Header: "x-api-key"}},
}

// failWatch makes kind's list or watch fail, as an unreachable API server does.
func failWatch(l *lab, kind watchhealth.Kind) {
	l.index.Health.RecordFailure(kind, errors.New("test: the watch failed"))
}

func newLab(t *testing.T) *lab {
	t.Helper()
	intercept, interceptRoot := makeCA(t, "interception")
	upstreamCA, upstreamRoot := makeCA(t, "upstream")
	name := "runner-" + strings.ReplaceAll(uuid.NewString(), "-", "")[:12]
	workload := workloadauthtesting.NewWorkload(t, kube, runner.Namespace, name)
	runner = workload.Principal.Account
	runnerPod = &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: runner.Namespace, Name: workload.Principal.PodName, UID: types.UID(workload.Principal.PodUID),
	}, Spec: corev1.PodSpec{ServiceAccountName: runner.Name}}
	var err error
	if hopToken, err = kube.PodToken(t.Context(), runnerPod, hopAudience); err != nil {
		t.Fatal(err)
	}
	if projectedToken, err = kube.PodToken(t.Context(), runnerPod, projectedAudience); err != nil {
		t.Fatal(err)
	}
	proxyCA := transporttesting.NewCA(t)
	proxyLeaf := proxyCA.Issue(t, environment_runtime_transport.EgressProxyURI)
	proxyTLS, err := proxyLeaf.WorkloadServerTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	proxyTLS.NextProtos = []string{"http/1.1"}
	clientLeaf := proxyCA.Issue(t, workload.URI)
	proxyClientTLS, err := clientLeaf.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	proxyClientTLS.NextProtos = []string{"http/1.1"}
	svids, err := resolver.New(workload.Objects, []string{runner.Namespace}, nil)
	if err != nil {
		t.Fatal(err)
	}
	l := &lab{
		index:          policy.NewIndex(),
		records:        &records{},
		roots:          interceptRoot,
		proxyClientTLS: proxyClientTLS,
		workload:       workload,
		upstreamCA:     upstreamCA,
		lookup:         func(string) []netip.Addr { return []netip.Addr{netip.MustParseAddr("127.0.0.1")} },
	}
	l.index.Health.MarkSynced()
	l.index.PutCredential(keyCredential)
	l.index.PutSecret(resources.NewSecret("keys", map[string]string{"key": "real-secret"}))
	l.allow(policyapi.Rule{
		Type: resources.EgressRuleType, Hosts: []string{fixtureHost}, CredentialRef: &policyapi.CredentialRef{Name: "key"},
		Paths: []string{"/allowed", "/stream", "/sse", "/ws", "/cancel"},
	})
	// These tests revoke and rebind tokens between requests, so every call reviews its bearer.
	projected, err := auth.NewResolver(kube.TokenReviews(), projectedAudience, []string{runner.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	authentication, err := identity.NewAuthenticator(listener.Config{
		ListenAddress: "127.0.0.1:8888",
		TLS:           &listener.TLS{CertFile: proxyLeaf.CertFile, KeyFile: proxyLeaf.KeyFile},
		Auth:          []listener.Authentication{&listener.SPIFFEAuth{CAFile: proxyCA.RootFile}},
	}, identity.NewVerifier(map[string]*auth.Resolver{projectedAudience: projected}), svids)
	if err != nil {
		t.Fatal(err)
	}
	l.options = Options{
		CA: intercept, UpstreamRoots: upstreamRoot, Policy: l.index, Record: l.records.record,
		Authentication: authentication,
		// Loopback addresses stand in for upstream hosts.
		Upstream: upstream.NewResolver(
			[]netip.Prefix{netip.MustParsePrefix("127.0.0.0/8")},
			upstream.WithLookup(func(_ context.Context, host string) ([]netip.Addr, error) {
				l.resolutions.Add(1)
				return l.lookup(host), nil
			}),
			upstream.WithClock(func() time.Time { return time.Now().Add(time.Duration(l.skew.Load())) }),
		),
	}
	l.start(t, proxyTLS)
	return l
}

// allow grants runner exactly rules, replacing what it was granted.
func (l *lab) allow(rules ...policyapi.Rule) {
	l.index.Rules.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: "egress", Namespace: "rules"}, Spec: policyapi.PolicySetSpec{Rules: rules},
	})
	l.index.Rules.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "runner", Namespace: "rules"},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.Subject{Namespace: runner.Namespace, Name: runner.Name}, PolicySets: []string{"egress"},
		},
	})
}

func (l *lab) start(t *testing.T, tlsConfig *tls.Config) {
	t.Helper()
	proxy, err := New(l.options)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	l.proxy, l.address = proxy, listener.Addr().String()
	go proxy.Serve(tls.NewListener(listener, tlsConfig))
	t.Cleanup(func() { proxy.Drain(context.Background()) })
}

func (l *lab) upstream(t *testing.T, name string, tlsEnabled bool, handler http.Handler) (*httptest.Server, *countedListener) {
	t.Helper()
	return l.upstreamOn(t, "127.0.0.1:0", name, tlsEnabled, handler)
}

// upstreamOn is upstream listening on address.
func (l *lab) upstreamOn(t *testing.T, address, name string, tlsEnabled bool, handler http.Handler) (*httptest.Server, *countedListener) {
	t.Helper()
	listener, err := net.Listen("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(handler)
	server.Listener.Close()
	counted := &countedListener{Listener: listener, closed: make(chan struct{}, 16)}
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
	return l.tunnelPresenting(t, port, tlsEnabled, "")
}

// tunnelPresenting opens a tunnel with the lab's SVID and, verbatim, extra untrusted hop headers.
func (l *lab) tunnelPresenting(t *testing.T, port string, tlsEnabled bool, extra string) (net.Conn, *http2.ClientConn) {
	t.Helper()
	connection, response := l.connect(t, fixtureHost+":"+port, extra)
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

// connect sends a raw CONNECT for authority with the header lines verbatim and returns the response;
// on a 200 the connection is the open tunnel.
func (l *lab) connect(t *testing.T, authority, headers string) (net.Conn, *http.Response) {
	t.Helper()
	return l.connectWithTLS(t, l.proxyClientTLS, authority, headers)
}

func (l *lab) connectWithTLS(t *testing.T, tlsConfig *tls.Config, authority, headers string) (net.Conn, *http.Response) {
	t.Helper()
	connection, err := tls.Dial("tcp", l.address, tlsConfig)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	if _, err := fmt.Fprintf(connection, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n%s\r\n", authority, authority, headers); err != nil {
		t.Fatal(err)
	}
	response, err := http.ReadResponse(bufio.NewReader(connection), &http.Request{Method: "CONNECT"})
	if err != nil {
		t.Fatal(err)
	}
	return connection, response
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

// tunnelStatus sends the allowed request through the authenticated tunnel.
func tunnelStatus(t *testing.T, client *http2.ClientConn, port string) int {
	t.Helper()
	req := request(bounded(t), port, "/allowed", true, nil)
	response, err := client.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, response.Body)
	response.Body.Close()
	return response.StatusCode
}

// valueOf is what pointer addresses, or the zero value of a record field that was never set.
func valueOf[T any](pointer *T) (value T) {
	if pointer != nil {
		value = *pointer
	}
	return value
}

// send sends a plain-HTTP proxy request to the fixture host on a connection of its own, with the hop
// bearer in header if the test supplies one.
func (l *lab) send(t *testing.T, method, port, path string, header http.Header, body io.Reader) *http.Response {
	t.Helper()
	return l.sendOn(t, l.client(), method, port, path, header, body)
}

// client returns a client whose plain-HTTP proxy requests share one connection to the proxy.
func (l *lab) client() *http.Client {
	return &http.Client{Transport: l.clientTransport(l.proxyClientTLS, &tls.Config{RootCAs: l.roots})}
}

func (l *lab) clientTransport(proxyTLS, originTLS *tls.Config) *http.Transport {
	proxyURL, _ := url.Parse("https://" + l.address)
	return &http.Transport{
		Proxy:           http.ProxyURL(proxyURL),
		TLSClientConfig: originTLS,
		DialTLSContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			if address != l.address {
				return nil, fmt.Errorf("unexpected TLS endpoint %q", address)
			}
			connection, err := (&net.Dialer{}).DialContext(ctx, network, address)
			if err != nil {
				return nil, err
			}
			return tls.Client(connection, proxyTLS.Clone()), nil
		},
	}
}

func (l *lab) clientWithoutSVID() *http.Client {
	tlsConfig := l.proxyClientTLS.Clone()
	tlsConfig.Certificates = nil
	tlsConfig.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
		return &tls.Certificate{}, nil
	}
	return &http.Client{Transport: l.clientTransport(tlsConfig, &tls.Config{RootCAs: l.roots})}
}

func (l *lab) sendOn(t *testing.T, client *http.Client, method, port, path string, header http.Header, body io.Reader) *http.Response {
	t.Helper()
	req, err := http.NewRequestWithContext(bounded(t), method, "http://"+fixtureHost+":"+port+path, body)
	if err != nil {
		t.Fatal(err)
	}
	for name, values := range header {
		req.Header[name] = values
	}
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { response.Body.Close() })
	return response
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
	// The CONNECT pinned the host; the denied request inside added neither a lookup nor a dial.
	if response.StatusCode != 403 || counted.accepts.Load() != 0 || l.resolutions.Load() != 1 {
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
			// Both requests go to the address the CONNECT pinned, on one pooled connection.
			if counted.accepts.Load() != 1 || l.resolutions.Load() != 1 {
				t.Fatalf("TCP=%d DNS=%d", counted.accepts.Load(), l.resolutions.Load())
			}
		})
	}
}

// get sends one request for path on a tunnel of its own and returns the body.
func (l *lab) get(t *testing.T, port, path string, encrypted bool) string {
	t.Helper()
	_, client := l.tunnel(t, port, encrypted)
	response, err := client.RoundTrip(request(bounded(t), port, path, encrypted, nil))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil || response.StatusCode != 200 {
		t.Fatalf("%s: status %d, %v", path, response.StatusCode, err)
	}
	return string(body)
}

func TestUpstreamConnectionIsReusedAcrossTunnelsForOnePinAndClosedOnDrain(t *testing.T) {
	for _, encrypted := range []bool{true, false} {
		t.Run(fmt.Sprint("tls=", encrypted), func(t *testing.T) {
			l := newLab(t)
			upstream, counted := l.upstream(t, fixtureHost, encrypted, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("done")) }))
			for range 3 {
				l.get(t, portOf(upstream), "/allowed", encrypted)
			}
			if counted.accepts.Load() != 1 {
				t.Fatalf("%d upstream connections for one pin, want 1", counted.accepts.Load())
			}
			if err := l.proxy.Drain(bounded(t)); err != nil {
				t.Fatal(err)
			}
			select {
			case <-counted.closed:
			case <-time.After(5 * time.Second):
				t.Fatal("idle upstream connection stayed open after drain")
			}
		})
	}
}

func TestUpstreamConnectionIsNotReusedOnceTheHostIsPinnedElsewhere(t *testing.T) {
	l := newLab(t)
	handler := func(answer string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(answer)) })
	}
	first, firstConnections := l.upstream(t, fixtureHost, true, handler("first"))
	_, secondConnections := l.upstreamOn(t, net.JoinHostPort("127.0.0.2", portOf(first)), fixtureHost, true, handler("second"))
	var address atomic.Pointer[netip.Addr]
	address.Store(new(netip.MustParseAddr("127.0.0.1")))
	l.lookup = func(string) []netip.Addr { return []netip.Addr{*address.Load()} }
	if body := l.get(t, portOf(first), "/allowed", true); body != "first" {
		t.Fatalf("first admission reached %q", body)
	}
	// The pin expires and the host now resolves elsewhere: the next request goes there, although a
	// connection to the old address sits idle.
	address.Store(new(netip.MustParseAddr("127.0.0.2")))
	l.skew.Store(int64(time.Hour))
	if body := l.get(t, portOf(first), "/allowed", true); body != "second" {
		t.Fatalf("re-pinned admission reached %q", body)
	}
	if firstConnections.accepts.Load() != 1 || secondConnections.accepts.Load() != 1 {
		t.Fatalf("connections: first=%d second=%d", firstConnections.accepts.Load(), secondConnections.accepts.Load())
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
	l.lookup = func(string) []netip.Addr {
		if calls.Add(1) > 1 {
			return []netip.Addr{netip.MustParseAddr("169.254.169.254")}
		}
		return []netip.Addr{netip.MustParseAddr("127.0.0.1")}
	}
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
	for scenario, want := range map[string]struct {
		status int
		reason decision.Reason
	}{
		"revoked-during-dns": {http.StatusBadGateway, decision.Unavailable},
		"mixed-answers":      {http.StatusForbidden, decision.AddressForbidden},
	} {
		t.Run(scenario, func(t *testing.T) {
			l := newLab(t)
			upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Error("denied DNS admission reached upstream") }))
			l.lookup = func(string) []netip.Addr {
				if scenario == "revoked-during-dns" {
					l.index.Rules.DeleteBinding("runner")
				}
				addresses := []netip.Addr{netip.MustParseAddr("127.0.0.1")}
				if scenario == "mixed-answers" {
					addresses = append(addresses, netip.MustParseAddr("::ffff:169.254.169.254"))
				}
				return addresses
			}
			response := l.send(t, "GET", portOf(upstream), "/allowed", nil, nil)
			refusal := l.records.all()[0]
			if response.StatusCode != want.status || *refusal.Reason != want.reason || counted.accepts.Load() != 0 {
				t.Fatalf("denied after DNS: status=%d reason=%v TCP=%d", response.StatusCode, *refusal.Reason, counted.accepts.Load())
			}
		})
	}
}

func TestExistingConnectionRevocationStalenessAndAuthCleanup(t *testing.T) {
	for change, c := range map[string]struct {
		status       int
		first, later decision.Reason
	}{
		"binding":             {http.StatusForbidden, decision.NoBinding, decision.NoBinding},
		"failing watch":       {http.StatusBadGateway, decision.Unavailable, decision.Unavailable},
		"revoked credential":  {http.StatusForbidden, decision.TokenRejected, decision.TokenRejected},
		"changed incarnation": {http.StatusForbidden, decision.TokenRejected, decision.TokenRejected},
	} {
		t.Run(change, func(t *testing.T) {
			l := newLab(t)
			upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("allowed")) }))
			_, client := l.tunnel(t, portOf(upstream), true)
			send := func() int { return tunnelStatus(t, client, portOf(upstream)) }
			if got := send(); got != 200 {
				t.Fatalf("initial request=%d", got)
			}
			switch change {
			case "binding":
				l.index.Rules.DeleteBinding("runner")
			case "failing watch":
				failWatch(l, policy.Secrets)
			case "revoked credential", "changed incarnation":
				if change == "revoked credential" {
					l.workload.Issue(t, "0123456789abcdef0123456789abcdef")
				} else {
					// The owner accepted another Pod: the one presenting this certificate is no longer it.
					record := l.workload.Record
					replacement := types.UID("00000000-0000-4000-8000-0000000000aa")
					record.Spec.Binding = &workloadapi.Binding{PodUID: replacement}
					record.Spec.Pod = &workloadapi.PodRef{Name: "test-replacement-pod", UID: replacement}
					if err := l.workload.Objects.Update(t.Context(), record); err != nil {
						t.Fatal(err)
					}
				}
			}
			if got := send(); got != c.status {
				t.Fatalf("revoked request=%d", got)
			}
			if got := send(); got != c.status {
				t.Fatalf("later request recovered stale auth/policy=%d", got)
			}
			if counted.accepts.Load() != 1 {
				t.Fatalf("denial opened upstream TCP: %d", counted.accepts.Load())
			}
			refusals := l.records.all()[2:]
			if len(refusals) != 2 || *refusals[0].Reason != c.first || *refusals[1].Reason != c.later {
				t.Fatalf("refusal reasons %v, want %v then %v", refusals, c.first, c.later)
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
	client := &http.Client{Transport: l.clientTransport(
		l.proxyClientTLS, &tls.Config{RootCAs: l.roots, NextProtos: []string{"http/1.1"}},
	)}
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

func TestManySequentialHTTP1RequestsOnOneTunnelAreEachDecidedOnTheirOwn(t *testing.T) {
	const requests = 12
	l := newLab(t)
	l.allow(rule("/items/*", ""))
	upstream, _ := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(r.URL.Path))
	}))
	// A TLS config of its own keeps the client on HTTP/1.1, which sends every request down the one tunnel.
	client := &http.Client{Transport: l.clientTransport(
		l.proxyClientTLS, &tls.Config{RootCAs: l.roots, NextProtos: []string{"http/1.1"}},
	)}
	t.Cleanup(client.CloseIdleConnections)

	for n := range requests {
		path := fmt.Sprintf("/items/%d", n)
		req, err := http.NewRequestWithContext(bounded(t), "GET", "https://"+fixtureHost+":"+portOf(upstream)+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(response.Body)
		response.Body.Close()
		if err != nil || response.ProtoMajor != 1 || response.StatusCode != 200 || string(body) != path {
			t.Fatalf("request %d: HTTP/%d status %d body %q, %v", n, response.ProtoMajor, response.StatusCode, body, err)
		}
	}

	records := l.records.all()
	if len(records) != requests+1 {
		t.Fatalf("%d decisions, want one CONNECT and %d requests", len(records), requests)
	}
	for i, record := range records {
		phase := decision.HTTPRequest
		if i == 0 {
			phase = decision.Connect
		}
		if record.Phase != phase || record.Outcome != decision.Allow || record.ConnectionID != records[0].ConnectionID {
			t.Errorf("decision %d: %+v", i, record)
		}
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
