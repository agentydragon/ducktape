package proxy

import (
	"bufio"
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"golang.org/x/net/http2"
	corev1 "k8s.io/api/core/v1"
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
	workloadCredential = resources.Credential{
		Name: "workload", Source: resources.AuthenticatedWorkloadToken{},
		Targets: []resources.Target{resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"}},
	}
	// bearerHeader carries hopToken as a plain proxy request's header, and validBearer as the
	// Proxy-Authorization line of a raw CONNECT. TestMain sets both.
	bearerHeader http.Header
	validBearer  string
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
			header("Proxy-Authorization", "Bearer "+hopToken, "Proxy-Authorization", "Bearer "+hopToken), "/allowed", nil,
			403, decision.TokenMissing, false,
		},
		"no rule for the path": {bearerHeader, "/elsewhere", nil, 403, decision.NoRule, true},
		"no binding": {
			bearerHeader, "/allowed", func(l *lab) { l.index.Rules.DeleteBinding("runner") }, 403, decision.NoBinding, true,
		},
		"another credential's placeholder": {
			header("Proxy-Authorization", "Bearer "+hopToken, "x-api-key", "agentplane-credential-other"), "/allowed",
			func(l *lab) {
				l.index.PutCredential(resources.Credential{
					Name: "other", Source: resources.SecretKey{Name: "keys", Key: "key"},
					Targets: []resources.Target{resources.WholeValue{Header: "x-api-key"}},
				})
			}, 403, decision.PlaceholderUnresolved, true,
		},
		"credential without a value": {
			header("Proxy-Authorization", "Bearer "+hopToken, "x-api-key", "agentplane-credential-key"), "/allowed",
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
		"failing policy watch": {
			bearerHeader, "/allowed", func(l *lab) { failWatch(l, reader.PolicyBindings) },
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
		header("Proxy-Authorization", "Bearer "+hopToken, "x-api-key", "agentplane-credential-key",
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
	proxyURL, err := url.Parse("http://" + l.address)
	if err != nil {
		t.Fatal(err)
	}
	get := func(client *http.Client, path string) {
		t.Helper()
		req, err := http.NewRequestWithContext(bounded(t), "GET", "http://"+fixtureHost+":"+portOf(upstream)+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		req.Header["Proxy-Authorization"] = bearerHeader["Proxy-Authorization"]
		response, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
	}
	first := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}}
	second := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)}}
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
	json := header("Proxy-Authorization", "Bearer "+hopToken, "Content-Type", "application/json")

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
				header("Proxy-Authorization", "Bearer "+hopToken, "Authorization", "Bearer agentplane-credential-kube",
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
		"no bearer":            {"", "", nil, 403, decision.TokenMissing, false},
		"unreviewed token":     {"Proxy-Authorization: Bearer forged\r\n", "", nil, 403, decision.TokenRejected, false},
		"two bearers":          {validBearer + validBearer, "", nil, 403, decision.TokenMissing, false},
		"review that fails":    {validBearer, "", func(l *lab) { l.hop.unreachable.Store(true) }, 502, decision.Unavailable, false},
		"no binding":           {validBearer, "", func(l *lab) { l.index.Rules.DeleteBinding("runner") }, 403, decision.NoBinding, true},
		"no rule for the host": {validBearer, "other.test", nil, 403, decision.NoRule, true},
		"failing policy watch": {
			validBearer, "", func(l *lab) { failWatch(l, reader.PolicyBindings) },
			502, decision.Unavailable, true,
		},
		"private address under an exact-host rule": {validBearer, "", privateAddress, 403, decision.AddressForbidden, true},
		"private address under a blanket rule": {
			validBearer, "",
			func(l *lab) {
				privateAddress(l)
				l.allow(policyapi.Rule{Type: resources.EgressRuleType, Hosts: []string{"*"}})
			},
			403, decision.AddressForbidden, true,
		},
		"host that does not resolve": {
			validBearer, "", func(l *lab) { l.lookup = func(string) []netip.Addr { return nil } }, 502, decision.HostUnresolved, true,
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

	_, response := l.connect(t, fixtureHost+":"+portOf(upstream), validBearer)

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

			_, response := l.connect(t, fixtureHost+":443", validBearer)

			record := l.records.all()[0]
			if response.StatusCode != c.status || valueOf(record.Address) != c.address || valueOf(record.Reason) != c.reason {
				t.Errorf("status %d, record %+v, want %d with address %q, reason %q", response.StatusCode, record, c.status, c.address, c.reason)
			}
		})
	}
}

func TestARejectedOrRevokedBearerLeavesATunnelWithoutAnIdentity(t *testing.T) {
	for name, c := range map[string]struct {
		// change happens after the tunnel, opened on token bound to pod, has carried a request.
		change func(t *testing.T, pod *corev1.Pod, token string)
		hop    string
	}{
		"replacement the reviewer rejects": {nil, "Bearer forged"},
		"bearer revoked mid-tunnel": {func(t *testing.T, pod *corev1.Pod, token string) {
			if err := kube.Revoke(t.Context(), pod, token, hopAudience); err != nil {
				t.Fatal(err)
			}
		}, ""},
	} {
		t.Run(name, func(t *testing.T) {
			l := newLab(t)
			pod, token := runnerToken(t, hopAudience)
			upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
			port := portOf(upstream)
			_, client := l.tunnelAs(t, token, port, true, "")
			if got := tunnelStatus(t, client, port, ""); got != 200 {
				t.Fatalf("initial request=%d", got)
			}
			if c.change != nil {
				c.change(t, pod, token)
			}

			// The refused request leaves the tunnel nothing to fall back on, neither the CONNECT's bearer
			// nor the one a replacement displaced.
			if first, later := tunnelStatus(t, client, port, c.hop), tunnelStatus(t, client, port, ""); first != 403 || later != 403 {
				t.Errorf("refused request=%d, later request=%d, want 403 for both", first, later)
			}

			refusals := l.records.all()[2:]
			if len(refusals) != 2 || valueOf(refusals[0].Reason) != decision.TokenRejected ||
				valueOf(refusals[1].Reason) != decision.TokenMissing || refusals[0].Subject != nil || refusals[1].Subject != nil {
				t.Errorf("refusals %+v, want token-rejected then token-missing, neither naming a subject", refusals)
			}
			if counted.accepts.Load() != 1 {
				t.Errorf("a refusal opened upstream TCP: %d", counted.accepts.Load())
			}
		})
	}
}

func TestABearerlessRequestInheritsNothingFromAnAuthenticatedConnection(t *testing.T) {
	l := newLab(t)
	var reached atomic.Int64
	upstream := plainUpstream(t, func(http.ResponseWriter, *http.Request) { reached.Add(1) })
	port := portOf(upstream)
	if _, response := l.connect(t, fixtureHost+":"+port, validBearer); response.StatusCode != 200 {
		t.Fatalf("CONNECT: %s", response.Status)
	}
	plain := l.client()
	if admitted := l.sendOn(t, plain, "GET", port, "/allowed", bearerHeader, nil); admitted.StatusCode != 200 {
		t.Fatalf("authenticated request: %s", admitted.Status)
	}

	for _, c := range []struct {
		name string
		send func() *http.Response
	}{
		{"next request on the authenticated plain connection", func() *http.Response {
			return l.sendOn(t, plain, "GET", port, "/allowed", nil, nil)
		}},
		{"another plain connection", func() *http.Response { return l.send(t, "GET", port, "/allowed", nil, nil) }},
		{"another CONNECT", func() *http.Response {
			_, response := l.connect(t, fixtureHost+":"+port, "")
			return response
		}},
	} {
		if response := c.send(); response.StatusCode != 403 || response.Header.Get("x-agentplane-egress") != "denied; reason=token-missing" {
			t.Errorf("%s: %d %q", c.name, response.StatusCode, response.Header.Get("x-agentplane-egress"))
		}
	}
	records := l.records.all()
	if len(records) != 5 {
		t.Fatalf("%d decisions, want 5", len(records))
	}
	connections := map[string]bool{}
	for _, record := range records {
		connections[record.ConnectionID] = true
	}
	// The premise: the plain connection's second request ran on the connection of its first, and every
	// other decision on a connection of its own.
	if records[1].ConnectionID != records[2].ConnectionID || len(connections) != 4 {
		t.Errorf("connection IDs %v do not describe one reused plain connection and three others", connections)
	}
	for _, refusal := range records[2:] {
		if refusal.Outcome != decision.Deny || valueOf(refusal.Reason) != decision.TokenMissing || refusal.Subject != nil {
			t.Errorf("record %+v", refusal)
		}
	}
	if reached.Load() != 1 {
		t.Errorf("%d requests reached upstream, want only the authenticated one", reached.Load())
	}
}

func TestEachConnectionSubstitutesItsOwnWorkloadBearerAndNeverForwardsTheHopHeader(t *testing.T) {
	l := newLab(t)
	l.index.PutCredential(workloadCredential)
	l.allow(rule("/workload", workloadCredential.Name))
	_, tokenB := runnerToken(t, hopAudience)
	_, tokenC := runnerToken(t, hopAudience)
	var authorization, hop seen
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authorization.set(r.Header.Get("Authorization"))
		hop.set(r.Header.Get("Proxy-Authorization"))
	})
	tlsUpstream, _ := l.upstream(t, fixtureHost, true, handler)
	plain := plainUpstream(t, handler)
	_, tunnelA := l.tunnelAs(t, hopToken, portOf(tlsUpstream), true, "")
	_, tunnelB := l.tunnelAs(t, tokenB, portOf(tlsUpstream), true, "")
	placeholder := "Bearer " + workloadCredential.Placeholder()
	through := func(client *http2.ClientConn) int {
		req := request(bounded(t), portOf(tlsUpstream), "/workload", true, nil)
		req.Header.Del("x-api-key")
		req.Header.Set("Authorization", placeholder)
		response, err := client.RoundTrip(req)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		return response.StatusCode
	}

	var got []string
	for _, send := range []func() int{
		func() int { return through(tunnelA) },
		func() int { return through(tunnelB) },
		func() int {
			return l.send(t, "GET", portOf(plain), "/workload", header("Proxy-Authorization", "Bearer "+tokenC, "Authorization", placeholder), nil).StatusCode
		},
		func() int { return through(tunnelA) },
	} {
		if status := send(); status != 200 {
			t.Fatalf("request %d: status %d", len(got), status)
		}
		got = append(got, authorization.take())
		if forwarded := hop.take(); forwarded != "" {
			t.Errorf("request %d forwarded Proxy-Authorization %q", len(got)-1, forwarded)
		}
	}

	if want := []string{"Bearer " + hopToken, "Bearer " + tokenB, "Bearer " + tokenC, "Bearer " + hopToken}; !slices.Equal(got, want) {
		t.Errorf("upstream saw Authorization %q, want each request's own hop bearer %q", got, want)
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
	_, hopBearer := runnerToken(t, hopAudience)
	l.index.PutCredential(tokenCredential)
	l.index.PutSecret(resources.NewSecret("keys", map[string]string{"key": "real-secret", "body": bodySecret}))
	l.allow(rule("/json", tokenCredential.Name), rule("/public/**", ""))
	var received seen
	upstream, _ := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		received.set(string(body))
	}))
	port := portOf(upstream)

	if refused := l.send(t, "GET", port, "/public/x", header("Proxy-Authorization", "Bearer "+forgedBearer), nil); refused.StatusCode != 403 {
		t.Fatalf("forged bearer: status %d", refused.StatusCode)
	}
	_, client := l.tunnelAs(t, hopBearer, port, true, "X-Agentplane-Workload-Token: "+projectedAudience+" "+forgedProjected+"\r\n")
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
	for _, secret := range []string{forgedBearer, forgedProjected, hopBearer, pathSecret, querySecret, headerSecret, bodySecret} {
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
	connection, response := l.connect(t, authority, validBearer)
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
	connection, err := net.Dial("tcp", l.address)
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
