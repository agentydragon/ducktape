package rules

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"maps"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"testing"
	"testing/fstest"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/docs"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	hopAudience = "egress"
	namespace   = "agentplane-test"
	description = "a token for the bot account, which can write to its own repositories"
	secretValue = "the-real-credential"
	// unreachable makes a review fail rather than answer.
	unreachable = "unreachable"
)

// reviews answers TokenReviews from a table of token to the ServiceAccount it proves, counting them.
type reviews struct {
	accounts map[string]string
	calls    int
}

func (r *reviews) Create(_ context.Context, review *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	r.calls++
	account, known := r.accounts[review.Spec.Token]
	if account == unreachable {
		return nil, errors.New("API server unreachable")
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: known,
		Audiences:     []string{hopAudience},
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:" + namespace + ":" + account, UID: "sa-" + account,
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"pod-" + account}, "authentication.kubernetes.io/pod-uid": {"uid-" + account},
			},
		},
	}}, nil
}

var (
	githubCredential = resources.Credential{
		Name:        "github-pat",
		Description: description,
		Source:      resources.SecretKey{Name: "vault-entry", Key: "credential-key"},
		Targets: []resources.Target{
			resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"},
			resources.BasicPassword{Header: "Authorization"},
		},
	}
	githubRule = policyapi.Rule{
		Type: resources.EgressRuleType, Hosts: []string{"api.github.com"}, Methods: []string{"GET", "POST"},
		Paths: []string{"/repos/**"}, CredentialRef: &policyapi.CredentialRef{Name: githubCredential.Name},
	}
	openRule = policyapi.Rule{Type: resources.EgressRuleType, Hosts: []string{"*.example.com"}}
)

func policySet(name string, rules ...policyapi.Rule) *policyapi.PolicySet {
	return &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	}
}

func binding(account, name string, sets ...string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: policyapi.PolicyBindingSpec{
			Subject: policyapi.ServiceAccountRef{Namespace: namespace, Name: account}, PolicySets: sets,
		},
	}
}

// api is the rules API over index, reviewing the accounts table's tokens, with no documentation.
func api(t *testing.T, accounts map[string]string, index *policy.Index) (http.Handler, *reviews) {
	t.Helper()
	return apiServing(t, accounts, index, fstest.MapFS{})
}

func apiServing(t *testing.T, accounts map[string]string, index *policy.Index, agentDocs fs.FS) (http.Handler, *reviews) {
	t.Helper()
	reviewed := &reviews{accounts: accounts}
	hop, err := auth.NewResolver(reviewed, hopAudience, []string{namespace})
	if err != nil {
		t.Fatal(err)
	}
	handler, err := New(identity.NewVerifier(hop, nil), index, agentDocs)
	if err != nil {
		t.Fatal(err)
	}
	return handler, reviewed
}

// githubIndex grants the sandbox account `sb` the GitHub policy and `other` a policy of its own.
func githubIndex() *policy.Index {
	index := policy.NewIndex(0)
	index.PutPolicySet(policySet("github", githubRule, openRule))
	index.PutPolicySet(policySet("other-only", openRule))
	index.PutBinding(binding("sb", "b-sb", "github"))
	index.PutBinding(binding("other", "b-other", "other-only"))
	index.PutCredential(githubCredential)
	index.PutSecret(resources.NewSecret("vault-entry", map[string]string{"credential-key": secretValue}))
	return index
}

func get(handler http.Handler, path string, headers ...string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(http.MethodGet, path, nil)
	for i := 0; i < len(headers); i += 2 {
		request.Header.Add(headers[i], headers[i+1])
	}
	recorded := httptest.NewRecorder()
	handler.ServeHTTP(recorded, request)
	return recorded
}

func bearer(token string) []string { return []string{"Authorization", "Bearer " + token} }

func TestReadRulesTellsTheCallerHowToPresentEveryCredential(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-sb": "sb"}, githubIndex())

	response := get(handler, "/v1/rules", bearer("token-sb")...)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", response.Code)
	}
	var got EgressView
	if err := json.Unmarshal(response.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	want := EgressView{
		Subject: policyapi.ServiceAccountRef{Namespace: namespace, Name: "sb"},
		Policies: []PolicyView{{Name: "github", Rules: []RuleView{
			{
				Hosts: []string{"api.github.com"}, Methods: []string{"GET", "POST"}, Paths: []string{"/repos/**"},
				Credential: &CredentialView{
					Name: "github-pat", Description: description, Placeholder: "agentplane-credential-github-pat",
					Targets: []egressapi.Target{
						{Method: egressapi.SchemeToken, Header: "Authorization", Scheme: "Bearer"},
						{Method: egressapi.BasicPassword, Header: "Authorization"},
					},
				},
			},
			{Hosts: []string{"*.example.com"}},
		}}},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("view = %+v, want %+v", got, want)
	}
	// The view has to carry the placeholder for the absences below to mean anything.
	if !strings.Contains(response.Body.String(), "agentplane-credential-github-pat") {
		t.Fatal("the response does not hold the placeholder")
	}
	for _, forbidden := range []string{secretValue, "vault-entry", "credential-key", "secretRef", "token-sb"} {
		if strings.Contains(response.Body.String(), forbidden) {
			t.Errorf("%q reached a sandbox-readable response", forbidden)
		}
	}
}

func TestReadRulesNamesEachTargetByItsOwnFields(t *testing.T) {
	index := policy.NewIndex(0)
	index.PutPolicySet(policySet("all-targets", githubRule))
	index.PutBinding(binding("sb", "b", "all-targets"))
	index.PutCredential(resources.Credential{
		Name: "github-pat", Description: description, Source: resources.AuthenticatedWorkloadToken{},
		Targets: []resources.Target{
			resources.WholeValue{Header: "X-Key"},
			resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"},
			resources.BasicUsername{Header: "Authorization"},
			resources.BasicPassword{Header: "Authorization"},
			resources.BasicWhole{Header: "Authorization"},
			resources.JSONField{Field: "password"},
		},
	})
	handler, _ := api(t, map[string]string{"token-sb": "sb"}, index)

	response := get(handler, "/v1/rules", bearer("token-sb")...)

	var got struct {
		Policies []struct {
			Rules []struct {
				Credential struct{ Targets []map[string]string }
			}
		}
	}
	if err := json.Unmarshal(response.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	// The wire form clients read: the schema promises these fields for each method, and no other.
	want := []map[string]string{
		{"method": "wholeValue", "header": "X-Key"},
		{"method": "schemeToken", "header": "Authorization", "scheme": "Bearer"},
		{"method": "basicUsername", "header": "Authorization"},
		{"method": "basicPassword", "header": "Authorization"},
		{"method": "basicWhole", "header": "Authorization"},
		{"method": "jsonField", "field": "password"},
	}
	if targets := got.Policies[0].Rules[0].Credential.Targets; !reflect.DeepEqual(targets, want) {
		t.Errorf("targets = %v, want %v", targets, want)
	}
}

// at follows a dotted path of object keys and list indexes into decoded JSON.
func at(t *testing.T, document any, path string) any {
	t.Helper()
	if path == "" {
		return document
	}
	for _, step := range strings.Split(path, ".") {
		switch node := document.(type) {
		case map[string]any:
			value, present := node[step]
			if !present {
				t.Fatalf("no key %q on the way to %q", step, path)
			}
			document = value
		case []any:
			index, err := strconv.Atoi(step)
			if err != nil || index < 0 || index >= len(node) {
				t.Fatalf("no element %q on the way to %q", step, path)
			}
			document = node[index]
		default:
			t.Fatalf("%v is neither an object nor a list on the way to %q", node, path)
		}
	}
	return document
}

// The key names are what agents read off the wire. Decoding into the package's own structs ignores
// their case, so they are checked against the raw document.
func TestReadRulesWireKeysAreThoseAgentsRead(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-sb": "sb"}, githubIndex())
	var document any
	if err := json.Unmarshal(get(handler, "/v1/rules", bearer("token-sb")...).Body.Bytes(), &document); err != nil {
		t.Fatal(err)
	}

	ruleKeys := []string{"credential", "hosts", "methods", "paths"}
	for path, want := range map[string][]string{
		"":                              {"policies", "subject"},
		"subject":                       {"name", "namespace"},
		"policies.0":                    {"name", "rules"},
		"policies.0.rules.0":            ruleKeys,
		"policies.0.rules.0.credential": {"description", "name", "placeholder", "targets"},
		"policies.0.rules.0.credential.targets.0": {"header", "method", "scheme"},
		"policies.0.rules.0.credential.targets.1": {"header", "method"},
		"policies.0.rules.1":                      ruleKeys,
	} {
		object, ok := at(t, document, path).(map[string]any)
		if !ok {
			t.Errorf("%q is not a JSON object", path)
			continue
		}
		if got := slices.Sorted(maps.Keys(object)); !slices.Equal(got, want) {
			t.Errorf("keys at %q = %v, want %v", path, got, want)
		}
	}
	// A rule that sets none of these carries the keys as null instead of omitting them.
	for _, path := range []string{"methods", "paths", "credential"} {
		if got := at(t, document, "policies.0.rules.1."+path); got != nil {
			t.Errorf("policies.0.rules.1.%s = %v, want null", path, got)
		}
	}
}

func TestReadRulesKeepsAnEmptyListDistinctFromAnAbsentOne(t *testing.T) {
	index := policy.NewIndex(0)
	index.PutPolicySet(policySet("none", policyapi.Rule{
		Type: resources.EgressRuleType, Hosts: []string{"a.example.com"}, Methods: []string{}, Paths: []string{},
	}))
	index.PutBinding(binding("sb", "b", "none"))
	handler, _ := api(t, map[string]string{"token-sb": "sb"}, index)

	var got EgressView
	if err := json.Unmarshal(get(handler, "/v1/rules", bearer("token-sb")...).Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}

	// A rule that admits no method is not one that admits any.
	if rule := got.Policies[0].Rules[0]; rule.Methods == nil || rule.Paths == nil {
		t.Errorf("rule = %+v, want empty methods and paths present", rule)
	}
}

func TestReadRulesAnswersNoEgressWithAnEmptyList(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-nobody": "nobody"}, githubIndex())

	response := get(handler, "/v1/rules", bearer("token-nobody")...)

	var got struct {
		Subject  policyapi.ServiceAccountRef
		Policies json.RawMessage
	}
	if err := json.Unmarshal(response.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	// An empty list, not null: no egress is a normal state, not a missing answer.
	if want := (policyapi.ServiceAccountRef{Namespace: namespace, Name: "nobody"}); response.Code != http.StatusOK ||
		got.Subject != want || string(got.Policies) != "[]" {
		t.Errorf("%d %s", response.Code, response.Body)
	}
}

func TestReadRulesFailsClosedWithoutAProvenBearer(t *testing.T) {
	for name, c := range map[string]struct {
		headers     []string
		wantReviews int
	}{
		"no Authorization":   {nil, 0},
		"not a bearer":       {[]string{"Authorization", "Basic abc"}, 0},
		"two Authorizations": {[]string{"Authorization", "Bearer token-sb", "Authorization", "Bearer token-sb"}, 0},
		"a forged bearer":    {bearer("forged"), 1},
		"only a hop bearer":  {[]string{"Proxy-Authorization", "Bearer token-sb"}, 0},
	} {
		t.Run(name, func(t *testing.T) {
			handler, reviewed := api(t, map[string]string{"token-sb": "sb"}, githubIndex())

			response := get(handler, "/v1/rules", c.headers...)

			if response.Code != http.StatusUnauthorized || response.Header().Get("WWW-Authenticate") != "Bearer" {
				t.Errorf("status %d, challenge %q; want 401 with a Bearer challenge", response.Code, response.Header().Get("WWW-Authenticate"))
			}
			if reviewed.calls != c.wantReviews {
				t.Errorf("%d token reviews, want %d: an unusable header must not reach the API server", reviewed.calls, c.wantReviews)
			}
			if strings.Contains(response.Body.String(), "forged") {
				t.Errorf("the response echoes the rejected token: %s", response.Body)
			}
		})
	}
}

func TestReadRulesIsUnavailableWhenTheBearerCannotBeReviewed(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-broken": unreachable}, githubIndex())

	if response := get(handler, "/v1/rules", bearer("token-broken")...); response.Code != http.StatusServiceUnavailable {
		t.Errorf("status = %d, want 503: an unanswered review says nothing about the caller", response.Code)
	}
}

func TestReadRulesCannotBeAimedAtAnotherSubject(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-sb": "sb", "token-other": "other"}, githubIndex())
	request := httptest.NewRequest(http.MethodGet, "/v1/rules?namespace="+namespace+"&name=other", strings.NewReader(`{"name":"other"}`))
	request.Header.Set("Authorization", "Bearer token-sb")
	request.Header.Set("Proxy-Authorization", "Bearer token-other")
	request.Header.Set("X-Agentplane-Subject", "other")
	response := httptest.NewRecorder()

	handler.ServeHTTP(response, request)

	var got EgressView
	if err := json.Unmarshal(response.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Subject.Name != "sb" || len(got.Policies) != 1 || got.Policies[0].Name != "github" {
		t.Errorf("view = %+v, want sb's alone", got)
	}
	if strings.Contains(response.Body.String(), "other") {
		t.Errorf("another subject's policy reached the response: %s", response.Body)
	}
}

func TestOnlyTheAgentRoutesAreServed(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-sb": "sb"}, githubIndex())

	for _, path := range []string{"/", "/decisions", "/healthz", "/docs/agents/"} {
		if response := get(handler, path, bearer("token-sb")...); response.Code != http.StatusNotFound {
			t.Errorf("GET %s = %d, want 404", path, response.Code)
		}
	}
	request := httptest.NewRequest(http.MethodPost, "/v1/rules", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusMethodNotAllowed {
		t.Errorf("POST /v1/rules = %d, want 405", response.Code)
	}
}

func TestOpenAPIDescribesTheRulesEndpoint(t *testing.T) {
	handler, _ := api(t, nil, policy.NewIndex(0))

	response := get(handler, "/openapi.json")

	var document struct {
		OpenAPI string
		Paths   map[string]struct {
			Get struct {
				Responses map[string]struct {
					Content map[string]struct {
						Schema struct {
							Required   []string
							Properties map[string]json.RawMessage
						}
					}
				}
			}
		}
	}
	if err := json.Unmarshal(response.Body.Bytes(), &document); err != nil {
		t.Fatal(err)
	}
	ok := document.Paths["/v1/rules"].Get.Responses["200"].Content["application/json"].Schema
	if !strings.HasPrefix(document.OpenAPI, "3.") || len(ok.Properties) != 2 || len(ok.Required) != 2 {
		t.Errorf("document = %+v, want an OpenAPI 3 description of the view's subject and policies", document)
	}
}

// links finds the Markdown links of a document that point at a path of this API.
var links = regexp.MustCompile(`\[[^\]]+\]\(([^)]+)\)`)

func TestAgentDocumentationGraphIsServed(t *testing.T) {
	agentDocs, err := fs.Sub(docs.Agents, "agents")
	if err != nil {
		t.Fatal(err)
	}
	handler, _ := apiServing(t, nil, policy.NewIndex(0), agentDocs)

	pending, visited := []string{"/llms.txt"}, map[string]bool{}
	for len(pending) > 0 {
		path := pending[0]
		pending = pending[1:]
		if visited[path] {
			continue
		}
		visited[path] = true
		response := get(handler, path)
		if response.Code != http.StatusOK || !strings.HasPrefix(response.Header().Get("Content-Type"), "text/") ||
			!strings.HasPrefix(response.Body.String(), "# ") {
			t.Errorf("GET %s = %d %q %.20q, want a served text document that opens with a heading", path, response.Code, response.Header().Get("Content-Type"), response.Body)
		}
		for _, match := range links.FindAllStringSubmatch(response.Body.String(), -1) {
			target, err := url.Parse(match[1])
			if err == nil && target.Host == "" && strings.HasPrefix(target.Path, "/docs/agents/") {
				pending = append(pending, target.Path)
			}
		}
	}
	if len(visited) < 2 {
		t.Errorf("visited %v, want llms.txt and the guides it links", visited)
	}
}

func TestAgentDocumentationServesOnlyFiles(t *testing.T) {
	handler, _ := apiServing(t, nil, policy.NewIndex(0), fstest.MapFS{
		"llms.txt":         {Data: []byte("# index")},
		"guides/start.md":  {Data: []byte("# start")},
		"credentials.md":   {Data: []byte("# credentials")},
		"credentials.json": {Data: []byte("{}")},
	})

	for path, want := range map[string]int{
		"/llms.txt":                     http.StatusOK,
		"/docs/agents/credentials.md":   http.StatusOK,
		"/docs/agents/credentials.json": http.StatusOK,
		"/docs/agents/guides/start.md":  http.StatusOK,
		"/docs/agents/guides":           http.StatusNotFound,
		"/docs/agents/missing.md":       http.StatusNotFound,
	} {
		if response := get(handler, path); response.Code != want {
			t.Errorf("GET %s = %d, want %d", path, response.Code, want)
		}
	}
}
