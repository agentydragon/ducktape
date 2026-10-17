package proxy

import (
	"bufio"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/go/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/go/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/go/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
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
	bearerHeader = http.Header{"Proxy-Authorization": {"Bearer valid"}}
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
		"no bearer":        {nil, "/allowed", nil, 403, decision.TokenMissing, false},
		"unreviewed token": {header("Proxy-Authorization", "Bearer forged"), "/allowed", nil, 403, decision.TokenRejected, false},
		"two bearers": {
			header("Proxy-Authorization", "Bearer valid", "Proxy-Authorization", "Bearer valid"), "/allowed", nil,
			403, decision.TokenMissing, false,
		},
		"no rule for the path": {bearerHeader, "/elsewhere", nil, 403, decision.NoRule, true},
		"no binding": {
			bearerHeader, "/allowed", func(l *lab) { l.index.DeleteBinding("runner") }, 403, decision.NoBinding, true,
		},
		"another credential's placeholder": {
			header("Proxy-Authorization", "Bearer valid", "x-api-key", "agentplane-credential-other"), "/allowed",
			func(l *lab) {
				l.index.PutCredential(resources.Credential{
					Name: "other", Source: resources.SecretKey{Name: "keys", Key: "key"},
					Targets: []resources.Target{resources.WholeValue{Header: "x-api-key"}},
				})
			}, 403, decision.PlaceholderUnresolved, true,
		},
		"credential without a value": {
			header("Proxy-Authorization", "Bearer valid", "x-api-key", "agentplane-credential-key"), "/allowed",
			func(l *lab) { l.index.DeleteSecret("keys") }, 403, decision.CredentialUnavailable, true,
		},
		"host that does not resolve": {
			bearerHeader, "/allowed", func(l *lab) { l.lookup = func(string) []netip.Addr { return nil } },
			502, decision.HostUnresolved, true,
		},
		"address inside the cluster": {
			bearerHeader, "/allowed",
			func(l *lab) {
				l.lookup = func(string) []netip.Addr { return []netip.Addr{netip.MustParseAddr("10.0.0.5")} }
			},
			403, decision.AddressForbidden, true,
		},
		"stale policy": {
			bearerHeader, "/allowed", func(l *lab) { l.index.RecordCycle(policy.Bindings, time.Now().Add(-2*staleAfter)) },
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

func TestAdmittedRequestIsSubstitutedAndLosesItsHopHeaders(t *testing.T) {
	l := newLab(t)
	upstream := plainUpstream(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-api-key") != "real-secret" || r.Header.Get("Proxy-Authorization") != "" ||
			r.Header.Get("X-Agentplane-Workload-Token") != "" {
			t.Errorf("upstream saw headers %v", r.Header)
		}
	})
	response := l.send(t, "GET", portOf(upstream), "/allowed",
		header("Proxy-Authorization", "Bearer valid", "x-api-key", "agentplane-credential-key",
			"X-Agentplane-Workload-Token", projectedAudience+" projected-valid"), nil)
	record := l.records.all()[0]
	if response.StatusCode != 200 || record.Outcome != decision.Allow || !record.Substituted ||
		*record.Address != "127.0.0.1" || *record.Binding != "runner" || *record.Policy != "egress" || *record.Rule != 0 ||
		*record.SourcePodUID != "uid-pod-1" || record.Subject.Name != "runner" || record.Host != fixtureHost {
		t.Errorf("status %d, record %+v", response.StatusCode, record)
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
	json := header("Proxy-Authorization", "Bearer valid", "Content-Type", "application/json")

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
	l.projected.bind("other-pod-token", "pod-2")
	var authorization seen
	upstream := plainUpstream(t, func(w http.ResponseWriter, r *http.Request) { authorization.set(r.Header.Get("Authorization")) })
	for name, c := range map[string]struct {
		presented     string
		status        int
		authorization string
	}{
		"same pod":              {projectedAudience + " projected-valid", 200, "Bearer projected-valid"},
		"another pod":           {projectedAudience + " other-pod-token", 403, ""},
		"unreviewed":            {projectedAudience + " forged", 403, ""},
		"unconfigured audience": {"https://other.test.invalid projected-valid", 403, ""},
		"malformed":             {"projected-valid", 403, ""},
	} {
		t.Run(name, func(t *testing.T) {
			response := l.send(t, "GET", portOf(upstream), "/kube",
				header("Proxy-Authorization", "Bearer valid", "Authorization", "Bearer agentplane-credential-kube",
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
		"X-Agentplane-Workload-Token: "+projectedAudience+" projected-valid\r\n")
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

	if status, got := send(), authorization.take(); status != 200 || got != "Bearer projected-valid" {
		t.Errorf("without the header: status %d, upstream Authorization %q", status, got)
	}
	// A request that presents the header with nothing usable in it inherits nothing.
	if status, got := send("malformed"), authorization.take(); status != 403 || got != "" {
		t.Errorf("with a malformed entry: status %d, upstream Authorization %q", status, got)
	}
	// What it presented replaces what the CONNECT did.
	if status, got := send(projectedAudience+" projected-valid"), authorization.take(); status != 200 || got != "Bearer projected-valid" {
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
	for ctx := bounded(t); l.index.Available(time.Now()) && ctx.Err() == nil; {
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
		connection, err := net.Dial("tcp", l.address)
		if err != nil {
			t.Fatal(err)
		}
		defer connection.Close()
		if _, err := io.WriteString(connection, "CONNECT "+authority+" HTTP/1.1\r\nHost: "+authority+"\r\n\r\n"); err != nil {
			t.Fatal(err)
		}
		response, err := http.ReadResponse(bufio.NewReader(connection), &http.Request{Method: "CONNECT"})
		if err != nil {
			t.Fatal(err)
		}
		if response.StatusCode != http.StatusBadRequest {
			t.Errorf("CONNECT %s: %s", authority, response.Status)
		}
	}
	if records := l.records.all(); len(records) != 0 {
		t.Errorf("malformed CONNECTs were decided: %+v", records)
	}
}
