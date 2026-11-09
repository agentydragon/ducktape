package proxy

import (
	"bufio"
	"bytes"
	"cmp"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
)

var (
	tokenCredential = resources.Credential{
		Name: "body-token", Source: resources.SecretKey{Name: "keys", Key: "body"},
		Targets: []resources.Target{resources.JSONField{Field: "token"}},
	}
	projectedCredential = resources.Credential{
		Name: "kube", Source: resources.ProjectedWorkloadToken{Audience: projectedAudience},
		Targets: []resources.Target{resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"}},
	}
)

func rule(path, credential string) policyapi.Rule {
	rule := policyapi.Rule{Type: resources.EgressRuleType, Hosts: []string{fixtureHost}, Paths: []string{path}}
	if credential != "" {
		rule.CredentialRef = &policyapi.CredentialRef{Name: credential}
	}
	return rule
}

// plainUpstream is an HTTP/1.1 destination for plain proxy requests.
func plainUpstream(t *testing.T, handler http.HandlerFunc) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return server
}

// seen is what an upstream handler observed, handed to the test goroutine.
type seen struct {
	mu    sync.Mutex
	value string
}

func (s *seen) set(value string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.value = value
}

// take returns what was observed since the last take.
func (s *seen) take() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	value := s.value
	s.value = ""
	return value
}

func header(pairs ...string) http.Header {
	h := http.Header{}
	for i := 0; i < len(pairs); i += 2 {
		h.Add(pairs[i], pairs[i+1])
	}
	return h
}

func TestRefusalsCarryTheirReasonAndAreRecorded(t *testing.T) {
	for name, c := range map[string]struct {
		header http.Header
		path   string
		setup  func(*lab)
		status int
		reason decision.Reason
		// authenticated is whether the record names a subject.
		authenticated bool
	}{
		"no rule for the path": {nil, "/elsewhere", nil, 403, decision.NoRule, true},
		"no binding": {
			nil, "/allowed", func(l *lab) { l.index.Rules.DeleteBinding("runner") }, 403, decision.NoBinding, true,
		},
		"another credential's placeholder": {
			header("x-api-key", "agentplane-credential-other"), "/allowed",
			func(l *lab) {
				l.index.PutCredential(resources.Credential{
					Name: "other", Source: resources.SecretKey{Name: "keys", Key: "key"},
					Targets: []resources.Target{resources.WholeValue{Header: "x-api-key"}},
				})
			}, 403, decision.PlaceholderUnresolved, true,
		},
		"credential without a value": {
			header("x-api-key", "agentplane-credential-key"), "/allowed",
			func(l *lab) { l.index.DeleteSecret("keys") }, 403, decision.CredentialUnavailable, true,
		},
		"host that does not resolve": {
			nil, "/allowed", func(l *lab) { l.lookup = func(string) []netip.Addr { return nil } },
			502, decision.HostUnresolved, true,
		},
		"address inside the cluster": {
			nil, "/allowed",
			func(l *lab) {
				l.lookup = func(string) []netip.Addr { return []netip.Addr{netip.MustParseAddr("10.0.0.5")} }
			},
			403, decision.AddressForbidden, true,
		},
		"failing policy watch": {
			nil, "/allowed", func(l *lab) { failWatch(l, reader.PolicyBindings) },
			502, decision.Unavailable, true,
		},
	} {
		t.Run(name, func(t *testing.T) {
			l := newLab(t)
			if c.setup != nil {
				c.setup(l)
			}
			upstream := plainUpstream(t, func(http.ResponseWriter, *http.Request) { t.Error("refusal reached upstream") })
			response := l.send(t, "GET", portOf(upstream), c.path, c.header, nil)
			refusal := l.records.all()[0]
			if response.StatusCode != c.status || response.Header.Get("x-agentplane-egress") != "denied; reason="+string(c.reason) {
				t.Errorf("response %d %q, want %d for %s", response.StatusCode, response.Header.Get("x-agentplane-egress"), c.status, c.reason)
			}
			if refusal.Outcome != decision.Deny || *refusal.Reason != c.reason || refusal.Phase != decision.HTTPRequest ||
				(refusal.Subject != nil) != c.authenticated || refusal.Address != nil {
				t.Errorf("record %+v", refusal)
			}
		})
	}
}

func TestProxyAuthorizationCannotAuthenticateWithoutAnSVID(t *testing.T) {
	l := newLab(t)
	upstream := plainUpstream(t, func(http.ResponseWriter, *http.Request) { t.Error("request without an SVID reached upstream") })
	request, err := http.NewRequestWithContext(bounded(t), http.MethodGet, "http://"+fixtureHost+":"+portOf(upstream)+"/allowed", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Proxy-Authorization", "Bearer "+hopToken)
	_, err = l.clientWithoutSVID().Do(request)
	if err == nil {
		t.Fatal("request without an SVID reached the HTTP proxy despite presenting a hop bearer")
	}
	if records := l.records.all(); len(records) != 0 {
		t.Fatalf("a connection without an SVID reached the proxy handler: %+v", records)
	}
}

func TestAdmittedRequestIsSubstitutedAndLosesItsHopHeaders(t *testing.T) {
	l := newLab(t)
	upstream := plainUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-api-key") != "real-secret" || r.Header.Get("Proxy-Authorization") != "" ||
			r.Header.Get("X-Agentplane-Workload-Token") != "" {
			t.Errorf("upstream saw headers %v", r.Header)
		}
	})
	response := l.send(t, "GET", portOf(upstream), "/allowed",
		header("x-api-key", "agentplane-credential-key",
			"X-Agentplane-Workload-Token", projectedAudience+" "+projectedToken), nil)
	record := l.records.all()[0]
	if response.StatusCode != 200 || record.Outcome != decision.Allow || !record.Substituted ||
		*record.Address != "127.0.0.1" || *record.Binding != "runner" || *record.Policy != "egress" || *record.Rule != 0 ||
		*record.SourcePodUID != string(runnerPod.UID) || record.Subject.Name != runner.Name || record.Host != fixtureHost {
		t.Errorf("status %d, record %+v", response.StatusCode, record)
	}
}

func TestPlainRequestsOnOneClientConnectionShareAConnectionID(t *testing.T) {
	l := newLab(t)
	upstream := plainUpstream(t, func(http.ResponseWriter, *http.Request) {})
	get := func(client *http.Client, path string) {
		t.Helper()
		req, err := http.NewRequestWithContext(bounded(t), "GET", "http://"+fixtureHost+":"+portOf(upstream)+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
	}
	first := l.client()
	second := l.client()
	get(first, "/allowed")
	get(first, "/refused")
	get(second, "/allowed")
	got := l.records.all()
	if len(got) != 3 {
		t.Fatalf("%d decisions, want 3", len(got))
	}
	if got[0].ConnectionID != got[1].ConnectionID || got[0].ConnectionID == got[2].ConnectionID {
		t.Errorf("connection IDs %q, %q, %q: want the first client's requests to share one, the second's to differ",
			got[0].ConnectionID, got[1].ConnectionID, got[2].ConnectionID)
	}
}

func TestJSONBodyCredentialIsSubstitutedWithItsLengthUpdated(t *testing.T) {
	l := newLab(t)
	l.index.PutCredential(tokenCredential)
	l.index.PutSecret(resources.NewSecret("keys", map[string]string{"key": "real-secret", "body": "a-much-longer-real-body-secret"}))
	l.allow(rule("/json", "body-token"), rule("/upload", ""))
	var received, length seen
	upstream := plainUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		received.set(string(body))
		length.set(strconv.FormatInt(r.ContentLength, 10))
	})
	json := header("Content-Type", "application/json")

	l.send(t, "POST", portOf(upstream), "/json", json, strings.NewReader(`{"token":"agentplane-credential-body-token","n":1}`))
	want := `{"token":"a-much-longer-real-body-secret","n":1}`
	if got, declared := received.take(), length.take(); got != want || declared != strconv.Itoa(len(want)) {
		t.Errorf("upstream got %q (%s bytes declared), want %q", got, declared, want)
	}

	// A body too large to inspect streams through whole when nothing in it is substituted.
	large := `{"padding":"` + strings.Repeat("x", 3*128*1024) + `"}`
	l.send(t, "POST", portOf(upstream), "/upload", json, strings.NewReader(large))
	if got := received.take(); got != large {
		t.Errorf("upstream got %d bytes of a %d byte body", len(got), len(large))
	}
}

func TestProjectedTokenIsSubstitutedOnlyWhenItProvesTheHopsPod(t *testing.T) {
	l := newLab(t)
	l.index.PutCredential(projectedCredential)
	l.allow(rule("/kube", "kube"))
	_, otherPodToken := runnerToken(t, projectedAudience)
	var authorization seen
	upstream := plainUpstream(t, func(w http.ResponseWriter, r *http.Request) { authorization.set(r.Header.Get("Authorization")) })
	for name, c := range map[string]struct {
		presented     string
		status        int
		authorization string
	}{
		"same pod":              {projectedAudience + " " + projectedToken, 200, "Bearer " + projectedToken},
		"another pod":           {projectedAudience + " " + otherPodToken, 403, ""},
		"unreviewed":            {projectedAudience + " forged", 403, ""},
		"unconfigured audience": {"https://other.test.invalid " + projectedToken, 403, ""},
		"malformed":             {projectedToken, 403, ""},
	} {
		t.Run(name, func(t *testing.T) {
			response := l.send(t, "GET", portOf(upstream), "/kube",
				header("Authorization", "Bearer agentplane-credential-kube",
					"X-Agentplane-Workload-Token", c.presented), nil)
			if got := authorization.take(); response.StatusCode != c.status || got != c.authorization {
				t.Errorf("status %d, upstream Authorization %q", response.StatusCode, got)
			}
			if c.status == 403 && response.Header.Get("x-agentplane-egress") != "denied; reason=credential-unavailable" {
				t.Errorf("refused as %q", response.Header.Get("x-agentplane-egress"))
			}
		})
	}
}

func TestTunnelRequestsInheritWhatTheConnectPresented(t *testing.T) {
	l := newLab(t)
	l.index.PutCredential(projectedCredential)
	l.allow(rule("/kube", "kube"))
	var authorization seen
	upstream, _ := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authorization.set(r.Header.Get("Authorization"))
	}))
	_, client := l.tunnelPresenting(t, portOf(upstream), true,
		"X-Agentplane-Workload-Token: "+projectedAudience+" "+projectedToken+"\r\n")
	send := func(tokens ...string) int {
		req := request(bounded(t), portOf(upstream), "/kube", true, nil)
		req.Header.Del("x-api-key")
		req.Header.Set("Authorization", "Bearer agentplane-credential-kube")
		req.Header["X-Agentplane-Workload-Token"] = tokens
		response, err := client.RoundTrip(req)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		return response.StatusCode
	}

	if status, got := send(), authorization.take(); status != 200 || got != "Bearer "+projectedToken {
		t.Errorf("without the header: status %d, upstream Authorization %q", status, got)
	}
	// A request that presents the header with nothing usable in it inherits nothing.
	if status, got := send("malformed"), authorization.take(); status != 403 || got != "" {
		t.Errorf("with a malformed entry: status %d, upstream Authorization %q", status, got)
	}
	// What it presented replaces what the CONNECT did.
	if status, got := send(projectedAudience+" "+projectedToken), authorization.take(); status != 200 || got != "Bearer "+projectedToken {
		t.Errorf("with a fresh entry: status %d, upstream Authorization %q", status, got)
	}
}

func TestDrainWaitsForRequestsInFlightButNotForIdleTunnels(t *testing.T) {
	l := newLab(t)
	started, release := make(chan struct{}), make(chan struct{})
	upstream, _ := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-release
		w.Write([]byte("finished"))
	}))
	idle, _ := l.tunnel(t, portOf(upstream), true)
	_, client := l.tunnel(t, portOf(upstream), true)
	type result struct {
		body string
		err  error
	}
	finished := make(chan result, 1)
	go func() {
		response, err := client.RoundTrip(request(bounded(t), portOf(upstream), "/allowed", true, nil))
		if err != nil {
			finished <- result{err: err}
			return
		}
		body, err := io.ReadAll(response.Body)
		response.Body.Close()
		finished <- result{string(body), err}
	}()
	<-started

	drained := make(chan error, 1)
	go func() { drained <- l.proxy.Drain(context.Background()) }()
	for ctx := bounded(t); l.index.Health.Available() && ctx.Err() == nil; {
		runtime.Gosched()
	}
	// Admission stops at once, while the request already in flight is left to finish.
	refused, err := client.RoundTrip(request(bounded(t), portOf(upstream), "/allowed", true, nil))
	if err != nil {
		t.Fatal(err)
	}
	refused.Body.Close()
	if refused.StatusCode != http.StatusBadGateway || refused.Header.Get("x-agentplane-egress") != "denied; reason=unavailable" {
		t.Errorf("request during drain: %d %q", refused.StatusCode, refused.Header.Get("x-agentplane-egress"))
	}
	select {
	case err := <-drained:
		t.Fatalf("Drain returned with a request in flight: %v", err)
	default:
	}

	close(release)
	if got := <-finished; got.err != nil || got.body != "finished" {
		t.Errorf("in-flight request: %q, %v", got.body, got.err)
	}
	if err := <-drained; err != nil {
		t.Errorf("Drain: %v", err)
	}
	idle.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := idle.Read(make([]byte, 1)); err == nil || strings.Contains(err.Error(), "timeout") {
		t.Errorf("idle tunnel was left open: %v", err)
	}
	if conn, err := net.Dial("tcp", l.address); err == nil {
		conn.Close()
		t.Error("proxy still accepts connections after Drain")
	}
}

func TestDrainGivesUpOnARequestThatNeverFinishes(t *testing.T) {
	l := newLab(t)
	started, release := make(chan struct{}), make(chan struct{})
	defer close(release)
	upstream, _ := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-release
	}))
	_, client := l.tunnel(t, portOf(upstream), true)
	go client.RoundTrip(request(bounded(t), portOf(upstream), "/allowed", true, nil))
	<-started

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := l.proxy.Drain(ctx); err == nil {
		t.Error("Drain reported success with a request still in flight")
	}
	if err := client.Ping(bounded(t)); err == nil {
		t.Error("tunnel survived a Drain that gave up")
	}
}

func TestConnectNeedsAHostAndPort(t *testing.T) {
	l := newLab(t)
	for _, authority := range []string{fixtureHost, ":443", fixtureHost + ":https"} {
		_, response := l.connect(t, authority, "")
		if response.StatusCode != http.StatusBadRequest {
			t.Errorf("CONNECT %s: %s", authority, response.Status)
		}
	}
	if records := l.records.all(); len(records) != 0 {
		t.Errorf("malformed CONNECTs were decided: %+v", records)
	}
}

func TestConnectRefusalsCarryTheirReasonAndAreRecorded(t *testing.T) {
	privateAddress := func(l *lab) {
		l.lookup = func(string) []netip.Addr { return []netip.Addr{netip.MustParseAddr("10.0.0.5")} }
	}
	for name, c := range map[string]struct {
		headers string
		// host is the CONNECT target's host; the fixture host when empty.
		host   string
		setup  func(*lab)
		status int
		reason decision.Reason
		// authenticated is whether the record names a subject.
		authenticated bool
	}{
		"no binding":           {"", "", func(l *lab) { l.index.Rules.DeleteBinding("runner") }, 403, decision.NoBinding, true},
		"no rule for the host": {"", "other.test", nil, 403, decision.NoRule, true},
		"failing policy watch": {
			"", "", func(l *lab) { failWatch(l, reader.PolicyBindings) },
			502, decision.Unavailable, true,
		},
		"private address under an exact-host rule": {"", "", privateAddress, 403, decision.AddressForbidden, true},
		"private address under a blanket rule": {
			"", "",
			func(l *lab) {
				privateAddress(l)
				l.allow(policyapi.Rule{Type: resources.EgressRuleType, Hosts: []string{"*"}})
			},
			403, decision.AddressForbidden, true,
		},
		"host that does not resolve": {
			"", "", func(l *lab) { l.lookup = func(string) []netip.Addr { return nil } }, 502, decision.HostUnresolved, true,
		},
	} {
		t.Run(name, func(t *testing.T) {
			l := newLab(t)
			if c.setup != nil {
				c.setup(l)
			}
			upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
				t.Error("refusal reached upstream")
			}))
			_, response := l.connect(t, cmp.Or(c.host, fixtureHost)+":"+portOf(upstream), c.headers)
			records := l.records.all()
			if len(records) != 1 {
				t.Fatalf("%d decisions, want 1", len(records))
			}
			refusal := records[0]
			if response.StatusCode != c.status || response.Header.Get("x-agentplane-egress") != "denied; reason="+string(c.reason) {
				t.Errorf("response %d %q, want %d for %s", response.StatusCode, response.Header.Get("x-agentplane-egress"), c.status, c.reason)
			}
			if refusal.Outcome != decision.Deny || valueOf(refusal.Reason) != c.reason || refusal.Phase != decision.Connect ||
				refusal.Method != http.MethodConnect || (refusal.Subject != nil) != c.authenticated || refusal.Address != nil {
				t.Errorf("record %+v", refusal)
			}
			if counted.accepts.Load() != 0 {
				t.Errorf("a refused CONNECT opened %d upstream TCP connections", counted.accepts.Load())
			}
		})
	}
}

func TestAConnectTheRulesAdmitIsAcknowledgedWithoutDialingAndRecorded(t *testing.T) {
	l := newLab(t)
	upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))

	_, response := l.connect(t, fixtureHost+":"+portOf(upstream), "")

	records := l.records.all()
	if len(records) != 1 {
		t.Fatalf("%d decisions, want 1", len(records))
	}
	admitted := records[0]
	if response.StatusCode != 200 || counted.accepts.Load() != 0 {
		t.Errorf("status %d, %d upstream TCP connections", response.StatusCode, counted.accepts.Load())
	}
	if admitted.Outcome != decision.Allow || admitted.Phase != decision.Connect || admitted.Method != http.MethodConnect ||
		admitted.Reason != nil || valueOf(admitted.Subject) != (decision.Subject{Namespace: runner.Namespace, Name: runner.Name}) ||
		valueOf(admitted.Binding) != "runner" || valueOf(admitted.Address) != "127.0.0.1" || admitted.Host != fixtureHost {
		t.Errorf("record %+v", admitted)
	}
}

func TestClusterInternalRuleLetsTheGateAdmitAPrivateAddress(t *testing.T) {
	for name, c := range map[string]struct {
		clusterInternal bool
		status          int
		address         string
		reason          decision.Reason
	}{
		"rule that declares its host cluster-internal": {true, 200, "10.0.0.5", ""},
		"rule that does not":                           {false, 403, "", decision.AddressForbidden},
	} {
		t.Run(name, func(t *testing.T) {
			l := newLab(t)
			l.lookup = func(string) []netip.Addr { return []netip.Addr{netip.MustParseAddr("10.0.0.5")} }
			l.allow(policyapi.Rule{Type: resources.EgressRuleType, Hosts: []string{fixtureHost}, ClusterInternal: c.clusterInternal})

			_, response := l.connect(t, fixtureHost+":443", "")

			record := l.records.all()[0]
			if response.StatusCode != c.status || valueOf(record.Address) != c.address || valueOf(record.Reason) != c.reason {
				t.Errorf("status %d, record %+v, want %d with address %q, reason %q", response.StatusCode, record, c.status, c.address, c.reason)
			}
		})
	}
}

func TestProxyAuthorizationCannotReplaceAMissingSVIDOnCONNECT(t *testing.T) {
	l := newLab(t)
	upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Error("request without SVID reached upstream") }))
	tlsConfig := l.proxyClientTLS.Clone()
	tlsConfig.Certificates = nil
	tlsConfig.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
		return &tls.Certificate{}, nil
	}
	ctx := bounded(t)
	connection, err := (&net.Dialer{}).DialContext(ctx, "tcp", l.address)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	if err := connection.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}
	tlsConnection := tls.Client(connection, tlsConfig)
	responseStatus := 0
	if err := tlsConnection.HandshakeContext(ctx); err == nil {
		port := portOf(upstream)
		_, writeErr := fmt.Fprintf(tlsConnection, "CONNECT %s:%s HTTP/1.1\r\nHost: %s:%s\r\n%s\r\n",
			fixtureHost, port, fixtureHost, port, "")
		if writeErr == nil {
			response, readErr := http.ReadResponse(bufio.NewReader(tlsConnection), &http.Request{Method: http.MethodConnect})
			if readErr == nil {
				responseStatus = response.StatusCode
				response.Body.Close()
			}
		}
	}
	if responseStatus == http.StatusOK {
		t.Fatal("CONNECT without an SVID succeeded despite its hop bearer")
	}
	if len(l.records.all()) != 0 || counted.accepts.Load() != 0 {
		t.Fatalf("missing SVID reached the HTTP handler or dialed upstream: records=%+v TCP=%d", l.records.all(), counted.accepts.Load())
	}
}

func TestSVIDAuthenticatesProxyRequestsWithoutHopBearerHeaders(t *testing.T) {
	l := newLab(t)
	var reached atomic.Int64
	upstream := plainUpstream(t, func(http.ResponseWriter, *http.Request) { reached.Add(1) })
	port := portOf(upstream)
	if _, response := l.connect(t, fixtureHost+":"+port, ""); response.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT without a hop bearer: %s", response.Status)
	}
	plain := l.client()
	if response := l.sendOn(t, plain, "GET", port, "/allowed", nil, nil); response.StatusCode != http.StatusOK {
		t.Fatalf("SVID-authenticated request without a hop bearer: %s", response.Status)
	}
	for _, send := range []func() *http.Response{
		func() *http.Response { return l.sendOn(t, plain, "GET", port, "/allowed", nil, nil) },
		func() *http.Response { return l.send(t, "GET", port, "/allowed", nil, nil) },
		func() *http.Response {
			_, response := l.connect(t, fixtureHost+":"+port, "")
			return response
		},
	} {
		if response := send(); response.StatusCode != http.StatusOK {
			t.Errorf("SVID-authenticated request without a hop bearer: %d %q", response.StatusCode, response.Header.Get("x-agentplane-egress"))
		}
	}
	records := l.records.all()
	if len(records) != 5 {
		t.Fatalf("%d decisions, want one CONNECT and four requests", len(records))
	}
	connections := map[string]bool{}
	for _, record := range records {
		connections[record.ConnectionID] = true
		if record.Outcome != decision.Allow || record.Subject == nil || record.Subject.Name != runner.Name {
			t.Errorf("record %+v", record)
		}
	}
	if records[1].ConnectionID != records[2].ConnectionID || len(connections) != 4 {
		t.Errorf("connection IDs %v do not describe one reused proxy connection and three others", connections)
	}
	if reached.Load() != 3 {
		t.Errorf("%d requests reached upstream, want three HTTP requests", reached.Load())
	}
}

// guardedBuffer collects log output that the proxy's goroutines write while the test reads.
type guardedBuffer struct {
	mu     sync.Mutex
	buffer bytes.Buffer
}

func (b *guardedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buffer.Write(p)
}

func (b *guardedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buffer.String()
}

func TestNoSecretReachesTheLogsOrTheRecordedDecisions(t *testing.T) {
	const (
		forgedBearer    = "forged-bearer-secret"
		forgedProjected = "forged-projected-secret"
		pathSecret      = "path-secret"
		querySecret     = "query-secret"
		headerSecret    = "header-secret"
		bodySecret      = "body-credential-secret"
	)
	// slog's default handler writes through the log package.
	var logs guardedBuffer
	previous := log.Writer()
	log.SetOutput(&logs)
	t.Cleanup(func() { log.SetOutput(previous) })
	l := newLab(t)
	l.index.PutCredential(tokenCredential)
	l.index.PutSecret(resources.NewSecret("keys", map[string]string{"key": "real-secret", "body": bodySecret}))
	l.allow(rule("/json", tokenCredential.Name), rule("/public/**", ""))
	var received seen
	upstream, _ := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		received.set(string(body))
	}))
	port := portOf(upstream)

	// An extra hop credential fails authentication before policy is evaluated.
	if refused := l.send(t, "GET", port, "/not-granted", header("Proxy-Authorization", "Bearer "+forgedBearer), nil); refused.StatusCode != 403 {
		body, _ := io.ReadAll(refused.Body)
		t.Fatalf("forged bearer: status %d headers=%v body=%q", refused.StatusCode, refused.Header, body)
	}
	_, client := l.tunnelPresenting(t, port, true, "X-Agentplane-Workload-Token: "+projectedAudience+" "+forgedProjected+"\r\n")
	public := request(bounded(t), port, "/public/"+pathSecret+"?token="+querySecret, true, nil)
	public.Header.Del("x-api-key")
	public.Header.Set("X-Test-Private", headerSecret)
	login := request(bounded(t), port, "/json", true, strings.NewReader(`{"token":"`+tokenCredential.Placeholder()+`"}`))
	login.Header.Del("x-api-key")
	login.Header.Set("Content-Type", "application/json")
	for _, req := range []*http.Request{public, login} {
		response, err := client.RoundTrip(req)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != 200 {
			t.Fatalf("%s: status %d", req.URL.Path, response.StatusCode)
		}
	}

	records, err := json.Marshal(l.records.all())
	if err != nil {
		t.Fatal(err)
	}
	// The absences below mean something only if the secret was spent and the proxy logged and recorded.
	if !strings.Contains(received.take(), bodySecret) || len(l.records.all()) != 4 || logs.String() == "" {
		t.Fatalf("substituted body not received, or %d decisions and %d bytes of logs", len(l.records.all()), len(logs.String()))
	}
	for _, secret := range []string{forgedBearer, forgedProjected, pathSecret, querySecret, headerSecret, bodySecret} {
		if strings.Contains(logs.String(), secret) {
			t.Errorf("%q reached the logs", secret)
		}
		if strings.Contains(string(records), secret) {
			t.Errorf("%q reached a recorded decision", secret)
		}
	}
}

// Only the outer CONNECT is decided on host alone: a rule that allows GET /allowed on a host does
// not let a CONNECT to it through from inside the tunnel.
func TestAConnectInsideATunnelIsNotRelayed(t *testing.T) {
	l := newLab(t)
	l.allow(policyapi.Rule{
		Type: resources.EgressRuleType, Hosts: []string{fixtureHost}, Methods: []string{"GET"}, Paths: []string{"/allowed"},
	})
	var connects atomic.Int64
	upstream := plainUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodConnect {
			connects.Add(1)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	authority := fixtureHost + ":" + portOf(upstream)
	connection, response := l.connect(t, authority, "")
	if response.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT: %s", response.Status)
	}
	if _, err := io.WriteString(connection, "CONNECT "+authority+" HTTP/1.1\r\nHost: "+authority+"\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	inner, err := http.ReadResponse(bufio.NewReader(connection), &http.Request{Method: http.MethodConnect})
	if err != nil {
		t.Fatal(err)
	}
	if connects.Load() != 0 || inner.StatusCode < 400 {
		t.Fatalf("inner CONNECT: status %d, %d reached the upstream", inner.StatusCode, connects.Load())
	}
}

// A port that is no port is a malformed request, refused before it is authenticated, decided or
// recorded: the history column cannot hold it, and one such row fails the whole batch it is in.
func TestAnOutOfRangePortIsRefusedUnrecorded(t *testing.T) {
	l := newLab(t)
	for _, port := range []string{"0", "65536", "4294967296"} {
		if _, response := l.connect(t, fixtureHost+":"+port, ""); response.StatusCode != http.StatusBadRequest {
			t.Errorf("CONNECT to port %s: %s", port, response.Status)
		}
	}
	connection, err := tls.Dial("tcp", l.address, l.proxyClientTLS)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	authority := fixtureHost + ":4294967296"
	if _, err := io.WriteString(connection, "GET http://"+authority+"/ HTTP/1.1\r\nHost: "+authority+"\r\n\r\n"); err != nil {
		t.Fatal(err)
	}
	response, err := http.ReadResponse(bufio.NewReader(connection), nil)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusBadRequest {
		t.Errorf("absolute-form request to an out-of-range port: %s", response.Status)
	}
	for _, record := range l.records.all() {
		t.Errorf("recorded %+v", record)
	}
}

func TestAdditionalHopCredentialsFailBeforePolicy(t *testing.T) {
	l := newLab(t)
	upstream := plainUpstream(t, func(http.ResponseWriter, *http.Request) { t.Error("ambiguous caller reached upstream") })
	for _, value := range []string{"Bearer " + hopToken, "Bearer forged", ""} {
		response := l.send(t, "GET", portOf(upstream), "/allowed", http.Header{"Proxy-Authorization": {value}}, nil)
		if response.StatusCode != http.StatusForbidden {
			t.Fatalf("ambiguous request returned %d", response.StatusCode)
		}
	}
	for _, record := range l.records.all() {
		if record.Subject != nil || record.Reason == nil || *record.Reason != decision.TokenRejected {
			t.Fatalf("additional credential reached policy: %+v", record)
		}
	}
}

// actAsAccount has the runner act as a ServiceAccount on an API server the lab's TLS upstream plays.
func actAsAccount(t *testing.T, l *lab, port int) resources.Credential {
	t.Helper()
	account := resources.Credential{
		Name:    "k8s-account",
		Source:  resources.KubernetesServiceAccount{Namespace: "accounts", Name: "diagnostics"},
		Targets: []resources.Target{resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"}},
	}
	l.index.PutCredential(account)
	l.index.LocalAPIServer = &policy.APIServer{
		Host: fixtureHost, Port: port, Token: func() (string, error) { return "proxy-own-token", nil },
	}
	l.allow(rule("/api/**", account.Name))
	return account
}

func TestATunnelToTheAPIServerActsAsTheAccountUntilTheRuleGoes(t *testing.T) {
	l := newLab(t)
	var authorization, impersonated seen
	upstream, _ := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authorization.set(r.Header.Get("Authorization"))
		impersonated.set(strings.Join(r.Header.Values("Impersonate-User"), ","))
	}))
	port, _ := strconv.Atoi(portOf(upstream))
	account := actAsAccount(t, l, port)
	_, client := l.tunnel(t, portOf(upstream), true)
	send := func(extra ...string) *http.Response {
		t.Helper()
		req := request(bounded(t), portOf(upstream), "/api/v1/namespaces", true, nil)
		req.Header.Del("x-api-key")
		req.Header.Set("Authorization", "Bearer "+account.Placeholder())
		for i := 0; i < len(extra); i += 2 {
			req.Header.Add(extra[i], extra[i+1])
		}
		response, err := client.RoundTrip(req)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		return response
	}

	if response := send(); response.StatusCode != 200 || authorization.take() != "Bearer proxy-own-token" ||
		impersonated.take() != "system:serviceaccount:accounts:diagnostics" {
		t.Fatalf("status %d: the API server did not see the proxy's bearer acting as the account", response.StatusCode)
	}
	admitted := l.records.all()[1]
	if admitted.Outcome != decision.Allow || !admitted.Substituted || admitted.Impersonated == nil ||
		*admitted.Impersonated != (decision.Subject{Namespace: "accounts", Name: "diagnostics"}) {
		t.Errorf("record %+v, want the impersonated account beside the subject", admitted)
	}

	// A caller asking for more on the proxy's rights is refused before anything is dialed.
	response := send("Impersonate-Group", "system:masters")
	if response.StatusCode != 403 || response.Header.Get("x-agentplane-egress") != "denied; reason=impersonation-forbidden" ||
		authorization.take() != "" {
		t.Errorf("caller-supplied Impersonate-Group: status %d, %q", response.StatusCode, response.Header.Get("x-agentplane-egress"))
	}
	if refused := l.records.all()[2]; refused.Outcome != decision.Deny || *refused.Reason != decision.ImpersonationForbidden {
		t.Errorf("record %+v", refused)
	}

	// The established tunnel decides each request again: the next one after the rule goes is refused.
	l.allow(rule("/api/**", ""))
	if response := send(); response.StatusCode != 403 || authorization.take() != "" || impersonated.take() != "" {
		t.Errorf("after revocation: status %d, the API server was reached", response.StatusCode)
	}
}

func TestTheAccountsBearerIsNotSentInPlaintext(t *testing.T) {
	l := newLab(t)
	upstream := plainUpstream(t, func(http.ResponseWriter, *http.Request) { t.Error("plaintext request reached the API server") })
	port, _ := strconv.Atoi(portOf(upstream))
	account := actAsAccount(t, l, port)
	response := l.send(t, "GET", portOf(upstream), "/api/v1/namespaces", header("Authorization", "Bearer "+account.Placeholder()), nil)
	if response.StatusCode != 403 || response.Header.Get("x-agentplane-egress") != "denied; reason=credential-unavailable" {
		t.Errorf("status %d, %q", response.StatusCode, response.Header.Get("x-agentplane-egress"))
	}
}
