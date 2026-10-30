package policy

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/presentation"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	namespace      = "agentplane-test"
	secretValue    = "real-value"
	appSecretValue = "real-app-value"
	authorization  = "Authorization"
	audience       = "https://kubernetes.test.invalid"
)

var (
	caller = policyapi.ServiceAccountRef{Namespace: namespace, Name: "sb"}
	bearer = resources.SchemeToken{Header: authorization, Scheme: "Bearer"}

	githubCredential = secretCredential("github-pat", "token", bearer, resources.BasicPassword{Header: authorization})
	appCredential    = secretCredential("app-token", "app", bearer)
	patSecret        = resources.NewSecret("pat", map[string]string{"token": secretValue, "app": appSecretValue})
)

func secretCredential(name, key string, targets ...resources.Target) resources.Credential {
	return resources.Credential{Name: name, Source: resources.SecretKey{Name: "pat", Key: key}, Targets: targets}
}

func egress(hosts []string, methods []string, paths []string, credential string) policyapi.Rule {
	rule := policyapi.Rule{Type: resources.EgressRuleType, Hosts: hosts, Methods: methods, Paths: paths}
	if credential != "" {
		rule.CredentialRef = &policyapi.CredentialRef{Name: credential}
	}
	return rule
}

var (
	githubRule = egress([]string{"api.github.com"}, []string{"GET", "POST"}, []string{"/repos/**"}, githubCredential.Name)
	appRule    = egress([]string{"api.github.com"}, []string{"GET", "POST"}, []string{"/repos/**"}, appCredential.Name)
	publicRule = egress([]string{"*.example.com"}, nil, []string{"/public/*"}, "")
	openRule   = egress([]string{"*"}, nil, nil, "")
)

func policySet(name string, rules ...policyapi.Rule) *policyapi.PolicySet {
	return &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace, Generation: 1},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	}
}

func binding(name string, sets ...string) *policyapi.PolicyBinding {
	return bindingFor(caller, name, sets...)
}

func bindingFor(subject policyapi.ServiceAccountRef, name string, sets ...string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace, Generation: 3},
		Spec:       policyapi.PolicyBindingSpec{Subject: subject, PolicySets: sets},
	}
}

func newIndex(
	sets []*policyapi.PolicySet, bindings []*policyapi.PolicyBinding, credentials []resources.Credential,
	secrets ...resources.Secret,
) *Index {
	index := NewIndex()
	for _, set := range sets {
		index.Rules.PutPolicySet(set)
	}
	for _, binding := range bindings {
		index.Rules.PutBinding(binding)
	}
	for _, credential := range credentials {
		index.PutCredential(credential)
	}
	for _, secret := range secrets {
		index.PutSecret(secret)
	}
	return index
}

// standardIndex holds the GitHub and app credentials and the Secret behind both.
func standardIndex(sets []*policyapi.PolicySet, bindings ...*policyapi.PolicyBinding) *Index {
	return newIndex(sets, bindings, []resources.Credential{githubCredential, appCredential}, patSecret)
}

func outbound(options ...func(*Request)) Request {
	request := Request{Method: "GET", Host: "api.github.com", Path: "/repos/o/r", Headers: http.Header{}}
	for _, option := range options {
		option(&request)
	}
	return request
}

func method(method string) func(*Request) { return func(r *Request) { r.Method = method } }
func host(host string) func(*Request)     { return func(r *Request) { r.Host = host } }
func path(path string) func(*Request)     { return func(r *Request) { r.Path = path } }
func header(name string, values ...string) func(*Request) {
	return func(r *Request) { r.Headers[http.CanonicalHeaderKey(name)] = values }
}

func connect(host string) Request {
	return Request{Method: http.MethodConnect, Host: host, Headers: http.Header{}}
}

func basic(payload string) string {
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(payload))
}

func swapped(value string) []presentation.HeaderRewrite {
	return []presentation.HeaderRewrite{{Header: authorization, Values: []string{"Bearer " + value}}}
}

func assertDecision(t *testing.T, got, want Decision) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Errorf("decision = %#v, want %#v", got, want)
	}
}

func TestEvaluate(t *testing.T) {
	base := standardIndex([]*policyapi.PolicySet{policySet("github", githubRule, publicRule)}, binding("b", "github"))
	twoCredentials := standardIndex([]*policyapi.PolicySet{policySet("tokens", githubRule, appRule)}, binding("b", "tokens"))
	broadAndCredentialed := func(bindings ...*policyapi.PolicyBinding) *Index {
		return standardIndex([]*policyapi.PolicySet{policySet("open", openRule), policySet("github", githubRule)}, bindings...)
	}
	placeholder := githubCredential.Placeholder()
	appPlaceholder := appCredential.Placeholder()
	withPlaceholder := header(authorization, "Bearer "+placeholder)

	for name, c := range map[string]struct {
		index   *Index
		request Request
		want    Decision
	}{
		"allow with substitution": {
			base, outbound(withPlaceholder), Allowed{Binding: "b", Policy: "github", Rule: 0, Rewrites: swapped(secretValue)},
		},
		"allow without placeholder forwards as-is": {base, outbound(), Allowed{Binding: "b", Policy: "github"}},
		"allow second rule on wildcard host": {
			base, outbound(host("a.b.example.com"), path("/public/x")), Allowed{Binding: "b", Policy: "github", Rule: 1},
		},
		"connect decided on host alone": {base, connect("api.github.com"), Allowed{Binding: "b", Policy: "github"}},
		"deny by method":                {base, outbound(method("DELETE")), Denied{decision.NoRule}},
		"deny by path":                  {base, outbound(path("/user")), Denied{decision.NoRule}},
		"deny wildcard apex":            {base, outbound(host("example.com"), path("/public/x")), Denied{decision.NoRule}},
		"deny unknown host":             {base, outbound(host("evil.example.org")), Denied{decision.NoRule}},
		"deny connect unknown host":     {base, connect("evil.example.org"), Denied{decision.NoRule}},
		"deny placeholder the matched rule does not substitute": {
			base,
			outbound(host("www.example.com"), path("/public/x"), withPlaceholder),
			Denied{decision.PlaceholderUnresolved},
		},
		"deny placeholder inside basic payload unsubstituted": {
			base,
			outbound(host("www.example.com"), path("/public/x"), header(authorization, basic("git:"+placeholder))),
			Denied{decision.PlaceholderUnresolved},
		},
		"a placeholder that is only a substring of the component is not presented": {
			base, outbound(header(authorization, "Bearer prefix-"+placeholder+"-suffix")),
			Allowed{Binding: "b", Policy: "github"},
		},
		"a placeholder under a scheme no target declares is not presented": {
			base, outbound(header(authorization, "Token "+placeholder)), Allowed{Binding: "b", Policy: "github"},
		},
		"a placeholder in a header no target names is not presented": {
			base, outbound(header("x-other", placeholder)), Allowed{Binding: "b", Policy: "github"},
		},
		"a basic payload with no colon is not the basicPassword target's shape": {
			base, outbound(header(authorization, basic(placeholder))), Allowed{Binding: "b", Policy: "github"},
		},
		"no binding": {
			standardIndex([]*policyapi.PolicySet{policySet("github", githubRule)}), outbound(), Denied{decision.NoBinding},
		},
		"binding for another sandbox": {
			standardIndex(
				[]*policyapi.PolicySet{policySet("github", githubRule)},
				bindingFor(policyapi.ServiceAccountRef{Namespace: namespace, Name: "other"}, "b", "github"),
			),
			outbound(),
			Denied{decision.NoBinding},
		},
		"missing policy": {standardIndex(nil, binding("b", "github")), outbound(), Denied{decision.NoBinding}},
		"missing policy beside a resolved one still grants the resolved": {
			standardIndex([]*policyapi.PolicySet{policySet("github", githubRule)}, binding("b", "absent", "github")),
			outbound(),
			Allowed{Binding: "b", Policy: "github"},
		},
		"credential secret missing denies rather than forwarding": {
			newIndex(
				[]*policyapi.PolicySet{policySet("github", githubRule)}, []*policyapi.PolicyBinding{binding("b", "github")},
				[]resources.Credential{githubCredential, appCredential},
			),
			outbound(withPlaceholder),
			Denied{decision.CredentialUnavailable},
		},
		"the credentialed rule decides though the broad binding sorts first": {
			broadAndCredentialed(binding("a-open", "open"), binding("b-github", "github")),
			outbound(withPlaceholder),
			Allowed{Binding: "b-github", Policy: "github", Rewrites: swapped(secretValue)},
		},
		"the credentialed rule decides though the broad binding sorts last": {
			broadAndCredentialed(binding("a-github", "github"), binding("b-open", "open")),
			outbound(withPlaceholder),
			Allowed{Binding: "a-github", Policy: "github", Rewrites: swapped(secretValue)},
		},
		"the credentialed rule decides though its policy is listed second": {
			broadAndCredentialed(binding("b", "open", "github")),
			outbound(withPlaceholder),
			Allowed{Binding: "b", Policy: "github", Rewrites: swapped(secretValue)},
		},
		"the credentialed rule decides though its policy is listed first": {
			broadAndCredentialed(binding("b", "github", "open")),
			outbound(withPlaceholder),
			Allowed{Binding: "b", Policy: "github", Rewrites: swapped(secretValue)},
		},
		"a request carrying no placeholder is allowed by the broad rule": {
			broadAndCredentialed(binding("a-open", "open"), binding("b-github", "github")),
			outbound(method("DELETE"), path("/user")),
			Allowed{Binding: "a-open", Policy: "open"},
		},
		"a placeholder the subject is not bound to is denied": {
			standardIndex([]*policyapi.PolicySet{policySet("github", githubRule), policySet("app", appRule)}, binding("b", "github")),
			outbound(header(authorization, "Bearer "+appPlaceholder)),
			Denied{decision.PlaceholderUnresolved},
		},
		"the placeholder picks the first of two credentialed rules": {
			twoCredentials, outbound(withPlaceholder),
			Allowed{Binding: "b", Policy: "tokens", Rule: 0, Rewrites: swapped(secretValue)},
		},
		"the placeholder picks the second of two credentialed rules": {
			twoCredentials, outbound(header(authorization, "Bearer "+appPlaceholder)),
			Allowed{Binding: "b", Policy: "tokens", Rule: 1, Rewrites: swapped(appSecretValue)},
		},
		"a rule naming a credential the namespace does not hold forwards untouched": {
			newIndex([]*policyapi.PolicySet{policySet("github", githubRule)}, []*policyapi.PolicyBinding{binding("b", "github")}, nil),
			outbound(withPlaceholder),
			Allowed{Binding: "b", Policy: "github"},
		},
	} {
		t.Run(name, func(t *testing.T) {
			assertDecision(t, c.index.Evaluate(caller, c.request, nil), c.want)
		})
	}
}

// A subject is a namespace and a name together: the proxy serves workloads from more than one
// namespace, so a binding that matched on the name alone would reach across them.
func TestTheSameNameInAnotherNamespaceIsADifferentSubject(t *testing.T) {
	bound := standardIndex([]*policyapi.PolicySet{policySet("github", githubRule)}, binding("b", "github"))
	elsewhere := policyapi.ServiceAccountRef{Namespace: "somewhere-else", Name: caller.Name}

	assertDecision(t, bound.Evaluate(caller, outbound(), nil), Allowed{Binding: "b", Policy: "github"})
	assertDecision(t, bound.Evaluate(elsewhere, outbound(), nil), Denied{decision.NoBinding})
}

func scopedIndex(credential resources.Credential) *Index {
	rule := egress([]string{"api.github.com"}, []string{"GET", "POST"}, []string{"/repos/**"}, credential.Name)
	return newIndex(
		[]*policyapi.PolicySet{policySet("scoped", rule)}, []*policyapi.PolicyBinding{binding("b", "scoped")},
		[]resources.Credential{credential},
	)
}

func TestAuthenticatedWorkloadSourceSubstitutesOnlyTheValidatedContextBearer(t *testing.T) {
	dynamic := resources.Credential{Name: "agentplane-workload", Source: resources.AuthenticatedWorkloadToken{}, Targets: []resources.Target{bearer}}
	scoped := scopedIndex(dynamic)
	token := "pod-a-authenticated-workload-token"
	request := outbound(header(authorization, "Bearer "+dynamic.Placeholder()))

	got := scoped.Evaluate(caller, request, &WorkloadContext{Bearer: token, Caller: caller, PodUID: "pod-a-uid"})

	assertDecision(t, got, Allowed{Binding: "b", Policy: "scoped", Rewrites: swapped(token)})
	for _, format := range []string{"%v", "%+v", "%#v"} {
		if strings.Contains(fmt.Sprintf(format, got), token) {
			t.Errorf("the decision prints the workload token under %s", format)
		}
	}

	assertDecision(t, scoped.Evaluate(caller, request, nil), Denied{decision.CredentialUnavailable})
	stale := &WorkloadContext{Bearer: "stale-token", Caller: policyapi.ServiceAccountRef{Namespace: namespace, Name: "someone-else"}}
	assertDecision(t, scoped.Evaluate(caller, request, stale), Denied{decision.CredentialUnavailable})
}

func TestProjectedWorkloadSource(t *testing.T) {
	projected := resources.Credential{
		Name: "kubernetes-workload", Source: resources.ProjectedWorkloadToken{Audience: audience}, Targets: []resources.Target{bearer},
	}
	scoped := scopedIndex(projected)
	request := outbound(header(authorization, "Bearer "+projected.Placeholder()))
	context := func(subject policyapi.ServiceAccountRef, tokens map[string]string) *WorkloadContext {
		return &WorkloadContext{Bearer: "pod-a-hop-token", Caller: subject, PodUID: "pod-a-uid", Projected: tokens}
	}

	// The hop bearer is not what a destination validating its own audience will take, so a rule
	// naming an audience gets the token presented for that audience and no other.
	got := scoped.Evaluate(caller, request, context(caller, map[string]string{audience: "pod-a-api-server-token", "https://other.test.invalid": "not-this-one"}))
	assertDecision(t, got, Allowed{Binding: "b", Policy: "scoped", Rewrites: swapped("pod-a-api-server-token")})
	for _, token := range []string{"pod-a-api-server-token", "pod-a-hop-token"} {
		if strings.Contains(fmt.Sprintf("%+v", got), token) {
			t.Errorf("the decision prints %s", token)
		}
	}

	// A token the verifier dropped is absent rather than present and unusable, so the rule naming
	// it denies where it is used instead of spending some other audience's token.
	assertDecision(t, scoped.Evaluate(caller, request, nil), Denied{decision.CredentialUnavailable})
	assertDecision(t, scoped.Evaluate(caller, request, context(caller, nil)), Denied{decision.CredentialUnavailable})
	otherAudience := context(caller, map[string]string{"https://other.test.invalid": "not-this-one"})
	assertDecision(t, scoped.Evaluate(caller, request, otherAudience), Denied{decision.CredentialUnavailable})

	// Context belongs to the caller it authenticated as: one subject's projected tokens are never
	// substituted into another's request, however well-formed they are.
	someoneElse := policyapi.ServiceAccountRef{Namespace: namespace, Name: "someone-else"}
	assertDecision(t, scoped.Evaluate(caller, request, context(someoneElse, map[string]string{audience: "pod-b-api-server-token"})),
		Denied{decision.CredentialUnavailable})
}

func TestABasicUsernameTargetTakesTheHalfBeforeTheFirstColon(t *testing.T) {
	// What `https://<token>@github.com` sends.
	credential := secretCredential("github-pat", "token", resources.BasicUsername{Header: authorization})
	request := outbound(header(authorization, basic(credential.Placeholder()+":")))

	got := scopedIndexWithSecret(credential).Evaluate(caller, request, nil)

	assertDecision(t, got, Allowed{
		Binding: "b", Policy: "scoped",
		Rewrites: []presentation.HeaderRewrite{{Header: authorization, Values: []string{basic(secretValue + ":")}}},
	})
}

func TestAWholeValueTargetTakesTheHeaderEntire(t *testing.T) {
	// The shape an API key travels in: `x-api-key: <key>`, no scheme to strip.
	credential := secretCredential("github-pat", "token", resources.WholeValue{Header: "X-Api-Key"})

	got := scopedIndexWithSecret(credential).Evaluate(caller, outbound(header("x-api-key", credential.Placeholder())), nil)

	assertDecision(t, got, Allowed{
		Binding: "b", Policy: "scoped", Rewrites: []presentation.HeaderRewrite{{Header: "X-Api-Key", Values: []string{secretValue}}},
	})
}

func scopedIndexWithSecret(credential resources.Credential) *Index {
	index := scopedIndex(credential)
	index.PutSecret(patSecret)
	return index
}

func TestSubstitutionCoversEveryValueOfTheHeader(t *testing.T) {
	base := standardIndex([]*policyapi.PolicySet{policySet("github", githubRule)}, binding("b", "github"))
	request := outbound(header(authorization, "Bearer "+githubCredential.Placeholder(), "Bearer other"))

	assertDecision(t, base.Evaluate(caller, request, nil), Allowed{
		Binding: "b", Policy: "github",
		Rewrites: []presentation.HeaderRewrite{{Header: authorization, Values: []string{"Bearer " + secretValue, "Bearer other"}}},
	})
}

func TestHostMatches(t *testing.T) {
	for _, c := range []struct {
		pattern, host string
		want          bool
	}{
		{"api.github.com", "API.GitHub.com", true},
		{"*.github.com", "api.github.com", true},
		{"*.github.com", "a.b.github.com", true},
		{"*.github.com", "github.com", false},
		{"*.github.com", ".github.com", false},
		{"*.github.com", "evilgithub.com", false},
		{"*", "example.com", true},
		{"*", "a.b.example.net", true},
		{"*", "140.82.121.4", true},
		{"*", "2606:50c0:8000::153", true},
		{"*", "", false},
	} {
		if got := hostMatches(c.pattern, c.host); got != c.want {
			t.Errorf("hostMatches(%q, %q) = %v, want %v", c.pattern, c.host, got, c.want)
		}
	}
}

func TestABlanketRequiresABindingAndRespectsMethodAndPath(t *testing.T) {
	limited := policySet("public", egress([]string{"*"}, []string{"GET"}, []string{"/public/*"}, ""))
	index := standardIndex([]*policyapi.PolicySet{limited})
	onRoute := outbound(host("arbitrary.example.org"), path("/public/file"))

	assertDecision(t, index.Evaluate(caller, onRoute, nil), Denied{decision.NoBinding})

	index.Rules.PutBinding(binding("b", "public"))
	assertDecision(t, index.Evaluate(caller, onRoute, nil), Allowed{Binding: "b", Policy: "public"})
	assertDecision(t, index.Evaluate(caller, outbound(host("arbitrary.example.org"), path("/private")), nil), Denied{decision.NoRule})
	assertDecision(t, index.Evaluate(caller, outbound(host("arbitrary.example.org"), path("/public/file"), method("POST")), nil),
		Denied{decision.NoRule})
}

func TestABlanketDoesNotSendAKnownCredentialToAnotherHostOrPath(t *testing.T) {
	index := standardIndex(
		[]*policyapi.PolicySet{policySet("open", openRule), policySet("github", githubRule)}, binding("b", "open", "github"),
	)
	withPlaceholder := header(authorization, "Bearer "+githubCredential.Placeholder())
	for _, request := range []Request{
		outbound(host("untrusted.example.org"), withPlaceholder),
		outbound(path("/outside-github-rule"), withPlaceholder),
	} {
		assertDecision(t, index.Evaluate(caller, request, nil), Denied{decision.PlaceholderUnresolved})
	}
}

var matrixCredential = secretCredential("matrix-login", "token", resources.JSONField{Field: "password"})

const matrixPath = "/_matrix/client/v3/login"

func matrixIndex() *Index {
	return newIndex(
		[]*policyapi.PolicySet{policySet(
			"matrix",
			egress([]string{"matrix.test"}, []string{"POST"}, []string{matrixPath}, "matrix-login"),
			egress([]string{"matrix.test", "other.test"}, nil, nil, ""),
		)},
		[]*policyapi.PolicyBinding{binding("b", "matrix")},
		[]resources.Credential{matrixCredential}, patSecret,
	)
}

func matrixRequest(options ...func(*Request)) Request {
	request := outbound(method("POST"), host("matrix.test"), path(matrixPath), header("content-type", "application/json"))
	request.Body = []byte(`{"password":"` + matrixCredential.Placeholder() + `"}`)
	for _, option := range options {
		option(&request)
	}
	return request
}

func body(body string) func(*Request) { return func(r *Request) { r.Body = []byte(body) } }

func TestAJSONSecretIsSubstitutedOnlyAfterCredentialAuthorization(t *testing.T) {
	got, ok := matrixIndex().Evaluate(caller, matrixRequest(), nil).(Allowed)

	if !ok || got.BodyRewrite == nil {
		t.Fatalf("decision = %#v, want an Allowed with a body rewrite", got)
	}
	var rewritten map[string]string
	if err := json.Unmarshal(got.BodyRewrite.Content, &rewritten); err != nil || !reflect.DeepEqual(rewritten, map[string]string{"password": secretValue}) {
		t.Errorf("rewritten body = %s (%v), want the secret in place of the placeholder", got.BodyRewrite.Content, err)
	}
	if strings.Contains(fmt.Sprintf("%+v", got), secretValue) {
		t.Error("the decision prints the secret")
	}
}

func TestOtherwiseAllowedRequestsCannotRedeemJSONCredentialsOffRoute(t *testing.T) {
	for name, request := range map[string]Request{
		"method": matrixRequest(method("GET")), "host": matrixRequest(host("other.test")), "path": matrixRequest(path("/elsewhere")),
	} {
		t.Run(name, func(t *testing.T) {
			assertDecision(t, matrixIndex().Evaluate(caller, request, nil), Denied{decision.PlaceholderUnresolved})
		})
	}
}

func TestAJSONRouteFailsClosedForInvalidBodyMissingSecretOrRevokedBinding(t *testing.T) {
	index := matrixIndex()
	assertDecision(t, index.Evaluate(caller, matrixRequest(body("{broken")), nil), Denied{decision.InvalidBody})

	index.DeleteSecret("pat")
	assertDecision(t, index.Evaluate(caller, matrixRequest(), nil), Denied{decision.CredentialUnavailable})

	index.Rules.DeleteBinding("b")
	assertDecision(t, index.Evaluate(caller, matrixRequest(), nil), Denied{decision.NoBinding})
}

func TestNonJSONUploadsToUnrelatedRoutesAreUnchanged(t *testing.T) {
	assertDecision(t, matrixIndex().Evaluate(caller, matrixRequest(path("/upload"), body("not json")), nil),
		Allowed{Binding: "b", Policy: "matrix", Rule: 1})
}

func TestABodyCredentialIsKnownEvenWithoutABindingNamingIt(t *testing.T) {
	index := matrixIndex()
	index.Rules.PutPolicySet(policySet("matrix", egress([]string{"matrix.test"}, nil, nil, "")))

	assertDecision(t, index.Evaluate(caller, matrixRequest(), nil), Denied{decision.PlaceholderUnresolved})
}

func TestAPolicyBindingGrantsItsExistingSetsRulesBySetPosition(t *testing.T) {
	kubernetesRule := policyapi.Rule{Type: "kubernetes"}
	index := standardIndex(
		[]*policyapi.PolicySet{policySet("github", kubernetesRule, kubernetesRule, githubRule)}, binding("pb", "absent", "github"),
	)

	assertDecision(t, index.Evaluate(caller, outbound(header(authorization, "Bearer "+githubCredential.Placeholder())), nil),
		Allowed{Binding: "pb", Policy: "github", Rule: 2, Rewrites: swapped(secretValue)})
}

func TestAPolicyBindingWithoutEgressRulesForTheCallerIsNoBinding(t *testing.T) {
	for name, index := range map[string]*Index{
		"no egress rules": standardIndex(
			[]*policyapi.PolicySet{policySet("kubernetes-only", policyapi.Rule{Type: "kubernetes"})}, binding("pb", "kubernetes-only"),
		),
		"another subject": standardIndex(
			[]*policyapi.PolicySet{policySet("github", githubRule)},
			bindingFor(policyapi.ServiceAccountRef{Namespace: namespace, Name: "another"}, "pb", "github"),
		),
	} {
		t.Run(name, func(t *testing.T) {
			assertDecision(t, index.Evaluate(caller, outbound(), nil), Denied{decision.NoBinding})
		})
	}
}

// A set one of whose egress rules does not parse grants nothing, rather than part of what its
// author meant.
func TestARefusedPolicySetGrantsNothing(t *testing.T) {
	broken := policySet("github", githubRule, egress([]string{"*"}, nil, nil, "github-pat"))
	index := standardIndex([]*policyapi.PolicySet{policySet("github", githubRule)}, binding("b", "github"))
	assertDecision(t, index.Evaluate(caller, outbound(), nil), Allowed{Binding: "b", Policy: "github"})

	index.Rules.PutPolicySet(broken)

	assertDecision(t, index.Evaluate(caller, outbound(), nil), Denied{decision.NoBinding})
}

// The dial needs to know whether the rule that decided declared its hosts cluster-internal, so the
// flag comes from the deciding rule alone, whichever path decided.
func TestAllowedCarriesTheDecidingRulesClusterInternal(t *testing.T) {
	internal := func(rule policyapi.Rule) policyapi.Rule {
		rule.ClusterInternal = true
		return rule
	}
	anyGitHub := egress([]string{"api.github.com"}, nil, nil, "")
	withPlaceholder := outbound(header(authorization, "Bearer "+githubCredential.Placeholder()))

	for name, c := range map[string]struct {
		rules   []policyapi.Rule
		request Request
		want    Allowed
	}{
		"first match, rule declares it": {
			[]policyapi.Rule{internal(anyGitHub)}, outbound(), Allowed{Binding: "b", Policy: "p", ClusterInternal: true},
		},
		"first match, rule does not": {[]policyapi.Rule{anyGitHub}, outbound(), Allowed{Binding: "b", Policy: "p"}},
		"first match, a later matching rule declares it": {
			[]policyapi.Rule{anyGitHub, internal(anyGitHub)}, outbound(), Allowed{Binding: "b", Policy: "p"},
		},
		"credential, rule declares it": {
			[]policyapi.Rule{internal(githubRule)},
			withPlaceholder,
			Allowed{Binding: "b", Policy: "p", Rewrites: swapped(secretValue), ClusterInternal: true},
		},
		"credential, rule does not": {
			[]policyapi.Rule{githubRule}, withPlaceholder, Allowed{Binding: "b", Policy: "p", Rewrites: swapped(secretValue)},
		},
		"credential, an earlier matching rule declares it": {
			[]policyapi.Rule{internal(anyGitHub), githubRule},
			withPlaceholder,
			Allowed{Binding: "b", Policy: "p", Rule: 1, Rewrites: swapped(secretValue)},
		},
		"credential, the resolving rule declares it beside an earlier one that does not": {
			[]policyapi.Rule{anyGitHub, internal(githubRule)},
			withPlaceholder,
			Allowed{Binding: "b", Policy: "p", Rule: 1, Rewrites: swapped(secretValue), ClusterInternal: true},
		},
	} {
		t.Run(name, func(t *testing.T) {
			index := standardIndex([]*policyapi.PolicySet{policySet("p", c.rules...)}, binding("b", "p"))
			assertDecision(t, index.Evaluate(caller, c.request, nil), c.want)
		})
	}
}
