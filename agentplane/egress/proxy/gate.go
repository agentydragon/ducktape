package proxy

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"reflect"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/upstream"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/google/uuid"
	"k8s.io/apimachinery/pkg/types"
)

const proxyAuthorization = "Proxy-Authorization"

// deniedHeader carries the machine-readable reason of a refusal.
const deniedHeader = "x-agentplane-egress"

// authenticated is the SVID a connection proved.
type authenticated struct {
	subject auth.Subject
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
	req.Header.Del(identity.UntrustedWorkloadTokenHeader)
	result := p.decide(req, client, supplied, body, host, port)
	p.record(req, client, host, port, result)
	return result.decision, result.pin
}

func (p *Proxy) decide(
	req *http.Request, client *connection, supplied bool, body []byte, host string, port int,
) outcome {
	ctx := req.Context()
	who, err := p.authenticate(ctx, client, supplied)
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
	caller := policySubject(who.subject)
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
	first := p.options.Policy.Evaluate(caller, request)
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
		!reflect.DeepEqual(p.options.Policy.Evaluate(caller, request), first) {
		return refused(decision.Unavailable)
	}
	return outcome{decision: admitted, pin: pin, caller: who}
}

// authenticate re-resolves the connection's SVID on every request. `supplied` is a
// Proxy-Authorization header on the request, which this listener refuses rather than ignores.
func (p *Proxy) authenticate(ctx context.Context, client *connection, supplied bool) (*authenticated, error) {
	client.mu.Lock()
	defer client.mu.Unlock()
	previous := client.auth
	caller, err := p.options.Authentication.Authenticate(ctx, client.peerState, supplied)
	if err != nil {
		client.auth = nil
		return nil, err
	}
	// A tunnel's requests must still identify the same Pod incarnation as its CONNECT.
	if previous != nil && caller.Subject != previous.subject {
		client.auth = nil
		return nil, &identity.Rejection{Reason: decision.PodMismatch, Detail: "authenticated tunnel identity changed"}
	}
	client.auth = &authenticated{subject: caller.Subject}
	return client.auth, nil
}

// policySubject is whose bindings decide: an Identity's only while the live Identity of its name
// still has the UID its credential named.
func policySubject(subject auth.Subject) reader.Subject {
	switch subject := subject.(type) {
	case auth.Principal:
		return reader.ServiceAccount{Namespace: subject.Account.Namespace, Name: subject.Account.Name}
	case auth.IdentityPrincipal:
		return reader.Identity{Name: subject.Identity.Name, UID: types.UID(subject.Identity.UID)}
	default:
		panic(fmt.Sprintf("unknown subject %T", subject))
	}
}

// decisionSubject is the caller as history records it: by name, without an Identity's UID.
func decisionSubject(subject auth.Subject) decision.Subject {
	switch subject := subject.(type) {
	case auth.Principal:
		return decision.ServiceAccount{Namespace: subject.Account.Namespace, Name: subject.Account.Name}
	case auth.IdentityPrincipal:
		return decision.Identity{Name: subject.Identity.Name}
	default:
		panic(fmt.Sprintf("unknown subject %T", subject))
	}
}

// podUID is the Pod the caller's credential was accepted for; empty for a standing workload, which
// proves none.
func podUID(subject auth.Subject) string {
	switch subject := subject.(type) {
	case auth.Principal:
		return subject.PodUID
	case auth.IdentityPrincipal:
		return subject.PodUID
	default:
		panic(fmt.Sprintf("unknown subject %T", subject))
	}
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
		record.Subject = decisionSubject(result.caller.subject)
		// A standing workload's credential proves no Pod.
		if pod := podUID(result.caller.subject); pod != "" {
			record.SourcePodUID = &pod
		}
	}
	switch d := result.decision.(type) {
	case policy.Allowed:
		address := result.pin.Address.String()
		record.Outcome, record.Binding, record.Policy, record.Rule = decision.Allow, &d.Binding, &d.Policy, &d.Rule
		record.Substituted = len(d.Rewrites) > 0 || d.BodyRewrite != nil
		if d.Impersonated != nil {
			record.Impersonated = &decision.ServiceAccount{Namespace: d.Impersonated.Namespace, Name: d.Impersonated.Name}
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
