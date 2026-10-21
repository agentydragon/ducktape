package policy

import (
	"errors"
	"net/http"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/presentation"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
)

// Request is one admission as the proxy sees it: the connection target, never the Host header.
type Request struct {
	Method string
	Host   string
	// Path is the request target of a request inside a tunnel; a CONNECT has none.
	Path    string
	Headers http.Header
	// Body is the request body when a JSON credential could be presented in it, else nil.
	Body []byte
}

func (r Request) isConnect() bool { return r.Method == http.MethodConnect }

// WorkloadContext is credential material retained only after the proxy authenticated this
// request or tunnel. Its tokens are never printed.
type WorkloadContext struct {
	Bearer string
	Caller policyapi.ServiceAccountRef
	PodUID string
	// Projected maps an audience to the token the sidecar presented for it, each already reviewed as
	// belonging to the same Pod as the hop bearer. An audience that did not review is absent rather
	// than present and unusable, so a rule naming it denies `credential-unavailable` at the point
	// of use.
	Projected map[string]string
}

func (WorkloadContext) String() string   { return "WorkloadContext{redacted}" }
func (WorkloadContext) GoString() string { return "WorkloadContext{redacted}" }

// Decision is the result of Evaluate: Allowed or Denied.
type Decision interface{ isDecision() }

// Allowed is a request a rule admits, and what it is forwarded as.
type Allowed struct {
	Binding string
	Policy  string
	Rule    int
	// Rewrites are the headers to forward with the real credential in place of its placeholder.
	Rewrites []presentation.HeaderRewrite
	// BodyRewrite is the JSON body to forward in place of the one sent, or nil.
	BodyRewrite *presentation.BodyRewrite
	// ClusterInternal is whether the deciding rule declared its hosts cluster-internal, which the
	// dial needs: the address check happens where the connection is made, not here.
	ClusterInternal bool
}

// Denied is a request no rule admits, and why.
type Denied struct{ Reason decision.Reason }

func (Allowed) isDecision() {}
func (Denied) isDecision()  {}

// match is a rule that admits the request, and where the subject got it.
type match struct {
	binding string
	policy  string
	number  int
	rule    resources.Rule
}

// Evaluate fails closed: only a matching rule admits, and the placeholder the request presents
// picks which.
//
// A request presenting a known placeholder is decided by a matching rule naming exactly that
// credential; one presenting none is decided by the first matching rule and is forwarded as it
// came. So a placeholder is never forwarded, and widening what a subject may reach never takes a
// credential away from it.
func (i *Index) Evaluate(caller policyapi.ServiceAccountRef, request Request, workload *WorkloadContext) Decision {
	grants := i.Rules.Grants(caller)
	if len(grants) == 0 {
		return Denied{decision.NoBinding}
	}
	matches := matchingRules(grants, request)
	if len(matches) == 0 {
		return Denied{decision.NoRule}
	}
	i.mu.RLock()
	defer i.mu.RUnlock()
	// A CONNECT is decided on host alone: its headers belong to the tunnel, and the requests
	// inside it are decided one by one, which is where a target applies.
	var body *presentation.JSONObject
	if !request.isConnect() && i.anyJSONFieldCredential() {
		var err error
		body, err = presentation.ParseJSONBody(request.Headers, request.Body)
		if errors.Is(err, presentation.ErrInvalidJSONBody) && i.demandsBody(matches, request) {
			return Denied{decision.InvalidBody}
		}
	}
	presented := map[string]presentation.Presentation{}
	if !request.isConnect() {
		presented = i.presentedCredentials(request, body)
	}
	if len(presented) == 0 {
		first := matches[0]
		return Allowed{
			Binding: first.binding, Policy: first.policy, Rule: first.number, ClusterInternal: first.rule.ClusterInternal,
		}
	}
	resolving := -1
	for index, candidate := range matches {
		if resolves(candidate.rule, presented) {
			resolving = index
			break
		}
	}
	if resolving < 0 {
		return Denied{decision.PlaceholderUnresolved}
	}
	decided := matches[resolving]
	// `resolves` admitted exactly one presented credential, and every presented one is in the index.
	name := *decided.rule.CredentialRef
	credential := i.credentials[name]
	value, ok := i.credentialValue(credential, caller, workload)
	if !ok {
		return Denied{decision.CredentialUnavailable}
	}
	bodyRewrite, err := presented[name].BodyRewrite(value)
	if err != nil {
		return Denied{decision.CredentialUnavailable}
	}
	return Allowed{
		Binding:         decided.binding,
		Policy:          decided.policy,
		Rule:            decided.number,
		Rewrites:        presented[name].Rewrites(value),
		BodyRewrite:     bodyRewrite,
		ClusterInternal: decided.rule.ClusterInternal,
	}
}

// demandsBody reports whether an invalid body must fail the request: some matching rule authorizes
// a credential that a JSON field can carry and the request did not present it in a header target
// instead. Ordinary uploads and header-only credentials are not JSON-only.
func (i *Index) demandsBody(matches []match, request Request) bool {
	for _, candidate := range matches {
		if candidate.rule.CredentialRef == nil {
			continue
		}
		credential, ok := i.credentials[*candidate.rule.CredentialRef]
		if !ok || !credential.HasJSONFieldTarget() {
			continue
		}
		if _, presented := presentation.Present(credential, request.Headers, nil); !presented {
			return true
		}
	}
	return false
}

func (i *Index) anyJSONFieldCredential() bool {
	for _, credential := range i.credentials {
		if credential.HasJSONFieldTarget() {
			return true
		}
	}
	return false
}

// presentedCredentials returns every known credential this request presents, by name.
//
// Known is namespace-wide: any EgressCredential in the index counts, whether or not the subject
// is bound to an egress policy naming it, so a placeholder the subject was never granted is
// recognised and refused rather than forwarded.
func (i *Index) presentedCredentials(request Request, body *presentation.JSONObject) map[string]presentation.Presentation {
	presented := map[string]presentation.Presentation{}
	for name, credential := range i.credentials {
		if p, ok := presentation.Present(credential, request.Headers, body); ok {
			presented[name] = p
		}
	}
	return presented
}

// resolves reports whether the rule names exactly the one credential the request presents.
func resolves(rule resources.Rule, presented map[string]presentation.Presentation) bool {
	if rule.CredentialRef == nil || len(presented) != 1 {
		return false
	}
	_, ok := presented[*rule.CredentialRef]
	return ok
}

// credentialValue is the real value of a credential. Both token sources are the caller's own
// identity, so each is usable only while the context still belongs to the caller being decided; a
// Secret is the proxy's and needs no such binding.
func (i *Index) credentialValue(
	credential resources.Credential, caller policyapi.ServiceAccountRef, workload *WorkloadContext,
) (string, bool) {
	bound := workload != nil && workload.Caller == caller
	switch source := credential.Source.(type) {
	case resources.SecretKey:
		secret, ok := i.secrets[source.Name]
		if !ok {
			return "", false
		}
		return secret.Value(source.Key)
	case resources.ProjectedWorkloadToken:
		if !bound {
			return "", false
		}
		token, ok := workload.Projected[source.Audience]
		return token, ok
	case resources.AuthenticatedWorkloadToken:
		if !bound {
			return "", false
		}
		return workload.Bearer, true
	}
	return "", false
}

// matchingRules returns every rule that admits the request, in walk order: bindings by name, then
// each binding's sets and their rules as listed.
func matchingRules(grants []reader.Grant[resources.Rule], request Request) []match {
	var matches []match
	for _, granted := range grants {
		for _, set := range granted.Sets {
			for _, numbered := range set.Rules {
				if ruleMatches(numbered.Rule, request) {
					matches = append(matches, match{
						binding: granted.Binding, policy: set.Name, number: numbered.Number, rule: numbered.Rule,
					})
				}
			}
		}
	}
	return matches
}

// ruleMatches admits a CONNECT on host alone; the requests inside the tunnel are decided one by one.
func ruleMatches(rule resources.Rule, request Request) bool {
	hostMatched := false
	for _, pattern := range rule.Hosts {
		if hostMatches(pattern, request.Host) {
			hostMatched = true
			break
		}
	}
	if !hostMatched {
		return false
	}
	if request.isConnect() {
		return true
	}
	if rule.Methods != nil && !slices.ContainsFunc(rule.Methods, func(method string) bool {
		return strings.EqualFold(method, request.Method)
	}) {
		return false
	}
	if rule.Paths == nil {
		return true
	}
	path := request.Path
	if path == "" {
		path = "/"
	}
	for _, glob := range rule.Paths {
		if glob.Matches(path) {
			return true
		}
	}
	return false
}

// hostMatches: `*` admits any host; suffix wildcards match subdomains, not the apex;
// case-insensitive.
func hostMatches(pattern, host string) bool {
	if pattern == "*" {
		return host != ""
	}
	host, pattern = strings.ToLower(host), strings.ToLower(pattern)
	if domain, wildcard := strings.CutPrefix(pattern, "*."); wildcard {
		return strings.HasSuffix(host, "."+domain) && len(host) > len(domain)+1
	}
	return host == pattern
}
