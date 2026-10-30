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
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	"github.com/agentydragon/ducktape/agentplane/egress/upstream"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"golang.org/x/net/http2"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	fixtureHost       = "upstream.test"
	hopAudience       = "test-egress"
	projectedAudience = "https://kubernetes.test.invalid"
)

var (
	kube   *kubeapiserver.Server
	runner = auth.Account{Namespace: "test-workloads", Name: "test-runner"}
	// runnerPod is the Pod hopToken and projectedToken are bound to.
	runnerPod                *corev1.Pod
	hopToken, projectedToken string
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) (err error) {
		kube = server
		ctx := context.Background()
		if runnerPod, err = server.Pod(ctx, runner); err != nil {
			return err
		}
		if hopToken, err = server.PodToken(ctx, runnerPod, hopAudience); err != nil {
			return err
		}
		if projectedToken, err = server.PodToken(ctx, runnerPod, projectedAudience); err != nil {
			return err
		}
		bearerHeader = http.Header{"Proxy-Authorization": {"Bearer " + hopToken}}
		validBearer = "Proxy-Authorization: Bearer " + hopToken + "\r\n"
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

// hopReviews passes the proxy's hop reviews to the API server, unless a test injects one of two
// faults the API server cannot produce: being unreachable, or, rebound, answering that a token it
// accepts proves another Pod than it did before, where the API server binds a token to one Pod for
// life.
type hopReviews struct {
	auth.TokenReviews
	unreachable atomic.Bool
	rebound     atomic.Bool
}

func (h *hopReviews) Create(ctx context.Context, review *authenticationv1.TokenReview, options metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	if h.unreachable.Load() {
		return nil, errors.New("test API server unreachable")
	}
	answer, err := h.TokenReviews.Create(ctx, review, options)
	if err != nil || !h.rebound.Load() || !answer.Status.Authenticated {
		return answer, err
	}
	answer.Status.User.Extra["authentication.kubernetes.io/pod-name"] = authenticationv1.ExtraValue{"test-replacement-pod"}
	answer.Status.User.Extra["authentication.kubernetes.io/pod-uid"] = authenticationv1.ExtraValue{"test-replacement-pod-uid"}
	return answer, nil
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
	index      *policy.Index
	hop        *hopReviews
	records    *records
	options    Options
	proxy      *Proxy
	address    string
	roots      *x509.CertPool
	upstreamCA tls.Certificate
	// lookup answers the proxy's DNS lookups; replace it before sending traffic.
	lookup      func(host string) []netip.Addr
	resolutions atomic.Int64
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
	l := &lab{
		index:      policy.NewIndex(),
		hop:        &hopReviews{TokenReviews: kube.TokenReviews()},
		records:    &records{},
		roots:      interceptRoot,
		upstreamCA: upstreamCA,
		lookup:     func(string) []netip.Addr { return []netip.Addr{netip.MustParseAddr("127.0.0.1")} },
	}
	l.index.Health.MarkSynced()
	l.index.PutCredential(keyCredential)
	l.index.PutSecret(resources.NewSecret("keys", map[string]string{"key": "real-secret"}))
	l.allow(policyapi.Rule{
		Type: resources.EgressRuleType, Hosts: []string{fixtureHost}, CredentialRef: &policyapi.CredentialRef{Name: "key"},
		Paths: []string{"/allowed", "/stream", "/sse", "/ws", "/cancel"},
	})
	// These tests revoke and rebind tokens between requests, so every call reviews its bearer.
	hop, err := auth.NewResolver(l.hop, hopAudience, []string{runner.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	projected, err := auth.NewResolver(kube.TokenReviews(), projectedAudience, []string{runner.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	l.options = Options{
		CA: intercept, UpstreamRoots: upstreamRoot, Policy: l.index, Record: l.records.record,
		Identity: identity.NewVerifier(hop, map[string]*auth.Resolver{projectedAudience: projected}),
		Upstream: upstream.NewResolver([]netip.Prefix{netip.MustParsePrefix("127.0.0.1/32")}, upstream.WithLookup(
			func(_ context.Context, host string) ([]netip.Addr, error) {
				l.resolutions.Add(1)
				return l.lookup(host), nil
			},
		)),
	}
	l.start(t)
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
			Subject: policyapi.ServiceAccountRef{Namespace: runner.Namespace, Name: runner.Name}, PolicySets: []string{"egress"},
		},
	})
}

func (l *lab) start(t *testing.T) {
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
	go proxy.Serve(listener)
	t.Cleanup(func() { proxy.Drain(context.Background()) })
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
	return l.tunnelPresenting(t, port, tlsEnabled, "")
}

// tunnelPresenting opens a tunnel whose CONNECT carries the valid hop bearer and, verbatim, extra
// header lines.
func (l *lab) tunnelPresenting(t *testing.T, port string, tlsEnabled bool, extra string) (net.Conn, *http2.ClientConn) {
	t.Helper()
	return l.tunnelAs(t, hopToken, port, tlsEnabled, extra)
}

// tunnelAs opens a tunnel whose CONNECT carries the hop bearer token and, verbatim, extra header lines.
func (l *lab) tunnelAs(t *testing.T, token, port string, tlsEnabled bool, extra string) (net.Conn, *http2.ClientConn) {
	t.Helper()
	connection, response := l.connect(t, fixtureHost+":"+port, "Proxy-Authorization: Bearer "+token+"\r\n"+extra)
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
	connection, err := net.Dial("tcp", l.address)
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

// tunnelStatus sends the allowed request through the tunnel, presenting hop as its Proxy-Authorization
// unless hop is empty, and returns the response status.
func tunnelStatus(t *testing.T, client *http2.ClientConn, port, hop string) int {
	t.Helper()
	req := request(bounded(t), port, "/allowed", true, nil)
	if hop != "" {
		req.Header.Set("Proxy-Authorization", hop)
	}
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
	proxyURL, _ := url.Parse("http://" + l.address)
	return &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}}
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
			// Both requests reuse the address the CONNECT pinned.
			if counted.accepts.Load() != 2 || l.resolutions.Load() != 1 {
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
			response := l.send(t, "GET", portOf(upstream), "/allowed", bearerHeader, nil)
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
		"invalid-replacement": {http.StatusForbidden, decision.TokenMissing, decision.TokenMissing},
		"changed-principal":   {http.StatusForbidden, decision.PodMismatch, decision.TokenMissing},
	} {
		t.Run(change, func(t *testing.T) {
			l := newLab(t)
			upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("allowed")) }))
			_, client := l.tunnel(t, portOf(upstream), true)
			send := func(header string) int { return tunnelStatus(t, client, portOf(upstream), header) }
			if got := send(""); got != 200 {
				t.Fatalf("initial request=%d", got)
			}
			switch change {
			case "binding":
				l.index.Rules.DeleteBinding("runner")
			case "failing watch":
				failWatch(l, policy.Secrets)
			case "changed-principal":
				l.hop.rebound.Store(true)
			}
			header := ""
			if change == "invalid-replacement" {
				header = "invalid"
			}
			if got := send(header); got != c.status {
				t.Fatalf("revoked request=%d", got)
			}
			if got := send(""); got != c.status {
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
	proxyURL, _ := url.Parse("http://" + l.address)
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL), TLSClientConfig: &tls.Config{RootCAs: l.roots}, ProxyConnectHeader: bearerHeader}}
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
	proxyURL, err := url.Parse("http://" + l.address)
	if err != nil {
		t.Fatal(err)
	}
	// A TLS config of its own keeps the client on HTTP/1.1, which sends every request down the one tunnel.
	client := &http.Client{Transport: &http.Transport{
		Proxy: http.ProxyURL(proxyURL), ProxyConnectHeader: bearerHeader, TLSClientConfig: &tls.Config{RootCAs: l.roots},
	}}
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
