package proxy

import (
	"context"
	"errors"
	"log/slog"
	"maps"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/upstream"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
)

const proxyAuthorization = "Proxy-Authorization"

// deniedHeader carries the machine-readable reason of a refusal.
const deniedHeader = "x-agentplane-egress"

// authenticated is the SVID a connection proved, and what it may present for substitution.
type authenticated struct {
	principal auth.Principal
	// projected holds the tokens the sidecar offered, by audience, that were reviewed as this Pod's.
	projected map[string]string
}

func (authenticated) String() string   { return "authenticated{redacted}" }
func (authenticated) GoString() string { return "authenticated{redacted}" }

// outcome is a decision with what the record of it needs.
type outcome struct {
	decision policy.Decision
	pin      upstream.Pin
	// caller is nil on a refusal that never authenticated.
	caller *authenticated
}

// gate decides one request or CONNECT and records the decision. The hop headers are this proxy's
// alone: they are removed here, before anything else reads the request, whatever is decided.
func (p *Proxy) gate(req *http.Request, client *connection, body []byte, host string, port int) (policy.Decision, upstream.Pin) {
	_, supplied := req.Header[proxyAuthorization]
	req.Header.Del(proxyAuthorization)
	presented := takeProjectedTokens(req.Header)
	result := p.decide(req, client, supplied, presented, body, host, port)
	p.record(req, client, host, port, result)
	return result.decision, result.pin
}

// takeProjectedTokens removes the audience-tagged tokens from the request. It is nil when the
// request carried none at all, which is what a request inside a tunnel looks like and so inherits
// what the CONNECT presented; an empty map is a request that carried the header with nothing usable
// in it, which inherits nothing.
func takeProjectedTokens(header http.Header) map[string]string {
	values := header.Values(identity.ProjectedTokenHeader)
	if len(values) == 0 {
		return nil
	}
	header.Del(identity.ProjectedTokenHeader)
	return identity.ParseProjectedTokens(values)
}

func (p *Proxy) decide(
	req *http.Request, client *connection, supplied bool, presented map[string]string,
	body []byte, host string, port int,
) outcome {
	ctx := req.Context()
	who, err := p.authenticate(ctx, client, supplied, presented)
	if err != nil {
		var rejection *identity.Rejection
		if errors.As(err, &rejection) {
			slog.Info("identity rejected", "method", req.Method, "host", host, "port", port, "reason", rejection.Reason,
				"detail", rejection.Detail)
			return outcome{decision: policy.Denied{Reason: rejection.Reason}}
		}
		slog.Warn("authentication failed; refusing", "method", req.Method, "host", host, "port", port,
			"error", err)
		return outcome{decision: policy.Denied{Reason: decision.Unavailable}}
	}
	refused := func(reason decision.Reason) outcome {
		return outcome{decision: policy.Denied{Reason: reason}, caller: who}
	}
	if !p.options.Policy.Health.Available() {
		return refused(decision.Unavailable)
	}
	caller := policyapi.ServiceAccountRef{Namespace: who.principal.Account.Namespace, Name: who.principal.Account.Name}
	request := policy.Request{Method: req.Method, Host: host, Port: port, Headers: req.Header, Body: body}
	if server := p.options.Policy.LocalAPIServer; server != nil {
		// Both evaluations below must see one token, or a rotation between them would read as a
		// change of decision. Read lazily: most requests never substitute it.
		request.APIServerToken = sync.OnceValues(server.Token)
	}
	if req.Method != http.MethodConnect {
		request.Path = req.URL.RequestURI()
		// relay dials https with UpstreamRoots verification; nothing else is TLS to the upstream.
		request.TLS = req.URL.Scheme == "https"
	}
	workload := &policy.WorkloadContext{
		Caller: caller, PodUID: who.principal.PodUID, Projected: who.projected,
	}
	first := p.options.Policy.Evaluate(caller, request, workload)
	admitted, ok := first.(policy.Allowed)
	if !ok {
		return outcome{decision: first, caller: who}
	}
	pin, err := p.options.Upstream.Pin(ctx, host, port, admitted.ClusterInternal)
	switch {
	case errors.Is(err, upstream.ErrAddressForbidden):
		slog.Info("upstream refused", "method", req.Method, "host", host, "port", port, "error", err)
		return refused(decision.AddressForbidden)
	case errors.Is(err, upstream.ErrHostUnresolved):
		slog.Info("upstream refused", "method", req.Method, "host", host, "port", port, "error", err)
		return refused(decision.HostUnresolved)
	case err != nil:
		slog.Warn("address check failed; refusing", "method", req.Method, "host", host, "port", port, "error", err)
		return refused(decision.Unavailable)
	}
	// Identity and DNS awaited I/O. Reading policy and readiness again after the last await is what
	// keeps a revocation, expiry or watch failure during admission from authorizing the dial.
	if !p.options.Policy.Health.Available() ||
		!reflect.DeepEqual(p.options.Policy.Evaluate(caller, request, workload), first) {
		return refused(decision.Unavailable)
	}
	return outcome{decision: admitted, pin: pin, caller: who}
}

// authenticate re-resolves the connection's SVID on every request and binds the presented
// projected tokens to the Pod it proves. `supplied` is a Proxy-Authorization header on the request,
// which this listener refuses rather than ignores.
func (p *Proxy) authenticate(
	ctx context.Context, client *connection, supplied bool, presented map[string]string,
) (*authenticated, error) {
	client.mu.Lock()
	defer client.mu.Unlock()
	previous := client.auth
	caller, err := p.options.Authentication.Authenticate(ctx, client.peerState, supplied)
	if err != nil {
		client.auth = nil
		return nil, err
	}
	// A tunnel's requests must still identify the same Pod incarnation as its CONNECT.
	if previous != nil && caller.Principal != previous.principal {
		client.auth = nil
		return nil, &identity.Rejection{Reason: decision.PodMismatch, Detail: "authenticated tunnel identity changed"}
	}
	var projected map[string]string
	if presented != nil {
		projected = p.options.Authentication.VerifyProjected(ctx, caller, presented)
	} else if previous != nil {
		projected = maps.Clone(previous.projected)
	}
	client.auth = &authenticated{principal: caller.Principal, projected: projected}
	return client.auth, nil
}

func (p *Proxy) record(req *http.Request, client *connection, host string, port int, result outcome) {
	record := decision.Record{
		EventID:      uuid.New(),
		At:           time.Now(),
		ProducerID:   p.producer,
		ConnectionID: client.id,
		Phase:        decision.HTTPRequest,
		Method:       truncate(req.Method, 32),
		Host:         truncate(strings.ToLower(host), 253),
		Port:         port,
	}
	if req.Method == http.MethodConnect {
		record.Phase = decision.Connect
	}
	if result.caller != nil {
		record.Subject = &decision.Subject{
			Namespace: result.caller.principal.Account.Namespace, Name: result.caller.principal.Account.Name,
		}
		// A standing workload's credential proves no Pod.
		if result.caller.principal.PodUID != "" {
			record.SourcePodUID = &result.caller.principal.PodUID
		}
	}
	switch d := result.decision.(type) {
	case policy.Allowed:
		address := result.pin.Address.String()
		record.Outcome, record.Binding, record.Policy, record.Rule = decision.Allow, &d.Binding, &d.Policy, &d.Rule
		record.Substituted = len(d.Rewrites) > 0 || d.BodyRewrite != nil
		if d.Impersonated != nil {
			record.Impersonated = &decision.Subject{Namespace: d.Impersonated.Namespace, Name: d.Impersonated.Name}
		}
		record.Address = &address
	case policy.Denied:
		record.Outcome, record.Reason = decision.Deny, &d.Reason
	}
	p.options.Record(record)
}

// truncate keeps what fits the history columns, by characters.
func truncate(s string, limit int) string {
	if runes := []rune(s); len(runes) > limit {
		return string(runes[:limit])
	}
	return s
}

// deny refuses with the reason in a header. A request that could not be decided, or whose host did
// not resolve, is the proxy's failure to reach a verdict rather than the client's to be refused.
func deny(w http.ResponseWriter, reason decision.Reason) {
	w.Header().Set(deniedHeader, "denied; reason="+string(reason))
	status := http.StatusForbidden
	if reason == decision.Unavailable || reason == decision.HostUnresolved {
		status = http.StatusBadGateway
	}
	w.WriteHeader(status)
}
