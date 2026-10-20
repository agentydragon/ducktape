package rules

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/agentydragon/ducktape/agentplane/egress/docs"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/proto/egress_rules"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
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

// serviceOver is the rules service over index, reviewing the accounts table's tokens.
func serviceOver(t *testing.T, accounts map[string]string, index *policy.Index) (*Service, *reviews) {
	t.Helper()
	reviewed := &reviews{accounts: accounts}
	hop, err := auth.NewResolver(reviewed, hopAudience, []string{namespace})
	if err != nil {
		t.Fatal(err)
	}
	return NewService(identity.NewVerifier(hop, nil), index), reviewed
}

// api is the rules API over index, with no documentation: its facade forwards to a gRPC server over
// loopback, as in the proxy.
func api(t *testing.T, accounts map[string]string, index *policy.Index) (http.Handler, *reviews) {
	t.Helper()
	service, reviewed := serviceOver(t, accounts, index)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	egressrulespb.RegisterEgressRulesServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	rulesFacade, err := facade.NewHandler(ctx, listener.Addr().String(), egressrulespb.RegisterEgressRulesHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	return New(rulesFacade, fstest.MapFS{}, docs.OpenAPI), reviewed
}

// documentation is the rules API serving agentDocs, with no rules behind it.
func documentation(agentDocs fs.FS) http.Handler {
	return New(http.NotFoundHandler(), agentDocs, docs.OpenAPI)
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

// document decodes a response body as the JSON the agent reads.
func document(t *testing.T, response *httptest.ResponseRecorder) any {
	t.Helper()
	var decoded any
	if err := json.Unmarshal(response.Body.Bytes(), &decoded); err != nil {
		t.Fatalf("%v in %q", err, response.Body)
	}
	return decoded
}

// The names and enum values are what the agent guides tell agents to read, so they are checked
// against the raw document.
func TestFacadeAnswersTheRulesAsProtoJSON(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-sb": "sb"}, githubIndex())

	response := get(handler, "/v1/rules", bearer("token-sb")...)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", response.Code, response.Body)
	}
	decoded := document(t, response)
	for path, want := range map[string]any{
		"subject.namespace":                              namespace,
		"subject.name":                                   "sb",
		"policies.0.name":                                "github",
		"policies.0.rules.0.hosts.0":                     "api.github.com",
		"policies.0.rules.0.methods.values.1":            "POST",
		"policies.0.rules.0.paths.values.0":              "/repos/**",
		"policies.0.rules.0.credential.name":             "github-pat",
		"policies.0.rules.0.credential.description":      description,
		"policies.0.rules.0.credential.placeholder":      "agentplane-credential-github-pat",
		"policies.0.rules.0.credential.targets.0.method": "METHOD_SCHEME_TOKEN",
		"policies.0.rules.0.credential.targets.0.header": "Authorization",
		"policies.0.rules.0.credential.targets.0.scheme": "Bearer",
		"policies.0.rules.0.credential.targets.1.method": "METHOD_BASIC_PASSWORD",
		"policies.0.rules.1.hosts.0":                     "*.example.com",
		"policies.0.rules.1.methods":                     nil,
		"policies.0.rules.1.paths":                       nil,
		"policies.0.rules.1.credential":                  nil,
	} {
		if got := at(t, decoded, path); got != want {
			t.Errorf("%s = %v, want %v", path, got, want)
		}
	}
	// The placeholder is present above, so these absences mean something.
	for _, forbidden := range []string{secretValue, "vault-entry", "credential-key", "secretRef", "token-sb"} {
		if strings.Contains(response.Body.String(), forbidden) {
			t.Errorf("%q reached a sandbox-readable response", forbidden)
		}
	}
}

func TestFacadeAnswersNoEgressWithAnEmptyList(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-nobody": "nobody"}, githubIndex())

	response := get(handler, "/v1/rules", bearer("token-nobody")...)

	// An empty list, not null: no egress is a normal state, not a missing answer.
	decoded := document(t, response)
	policies, ok := at(t, decoded, "policies").([]any)
	if response.Code != http.StatusOK || at(t, decoded, "subject.name") != "nobody" || !ok || len(policies) != 0 {
		t.Errorf("%d %s", response.Code, response.Body)
	}
}

func TestFacadeFailsClosedWithoutAProvenBearer(t *testing.T) {
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

			if code := at(t, document(t, response), "code"); response.Code != http.StatusUnauthorized || code != float64(codes.Unauthenticated) {
				t.Errorf("status %d, code %v; want 401 and Unauthenticated: %s", response.Code, code, response.Body)
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

func TestFacadeIsUnavailableWhenTheBearerCannotBeReviewed(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-broken": unreachable}, githubIndex())

	response := get(handler, "/v1/rules", bearer("token-broken")...)

	if code := at(t, document(t, response), "code"); response.Code != http.StatusServiceUnavailable || code != float64(codes.Unavailable) {
		t.Errorf("status %d, code %v; want 503 and Unavailable: an unanswered review says nothing about the caller", response.Code, code)
	}
}

func TestFacadeCannotBeAimedAtAnotherSubject(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-sb": "sb", "token-other": "other"}, githubIndex())
	request := httptest.NewRequest(http.MethodGet, "/v1/rules?namespace="+namespace+"&name=other", nil)
	request.Header.Set("Authorization", "Bearer token-sb")
	request.Header.Set("Proxy-Authorization", "Bearer token-other")
	request.Header.Set("X-Agentplane-Subject", "other")
	response := httptest.NewRecorder()

	handler.ServeHTTP(response, request)

	decoded := document(t, response)
	if at(t, decoded, "subject.name") != "sb" || at(t, decoded, "policies.0.name") != "github" {
		t.Errorf("view = %s, want sb's alone", response.Body)
	}
	if strings.Contains(response.Body.String(), "other") {
		t.Errorf("another subject's policy reached the response: %s", response.Body)
	}
}

func TestOnlyTheAgentRoutesAreServed(t *testing.T) {
	handler, _ := api(t, map[string]string{"token-sb": "sb"}, githubIndex())

	for _, path := range []string{"/", "/decisions", "/healthz", "/v1/decisions", "/docs/agents/"} {
		if response := get(handler, path, bearer("token-sb")...); response.Code != http.StatusNotFound {
			t.Errorf("GET %s = %d, want 404", path, response.Code)
		}
	}
	request := httptest.NewRequest(http.MethodPost, "/v1/rules", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code == http.StatusOK {
		t.Errorf("POST /v1/rules = %d, want a refusal", response.Code)
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

// links finds the Markdown links of a document that point at a path of this API.
var links = regexp.MustCompile(`\[[^\]]+\]\(([^)]+)\)`)

func TestAgentDocumentationGraphIsServed(t *testing.T) {
	agentDocs, err := fs.Sub(docs.Agents, "agents")
	if err != nil {
		t.Fatal(err)
	}
	handler := documentation(agentDocs)

	pending, visited := []string{"/llms.txt"}, map[string]bool{}
	for len(pending) > 0 {
		path := pending[0]
		pending = pending[1:]
		if visited[path] {
			continue
		}
		visited[path] = true
		response := get(handler, path)
		if strings.HasSuffix(path, ".json") {
			// The generated OpenAPI document of the rules route.
			if response.Code != http.StatusOK || response.Header().Get("Content-Type") != "application/json" || !json.Valid(response.Body.Bytes()) {
				t.Errorf("GET %s = %d %q %.20q, want a served JSON document", path, response.Code, response.Header().Get("Content-Type"), response.Body)
			}
		} else if response.Code != http.StatusOK || !strings.HasPrefix(response.Header().Get("Content-Type"), "text/") ||
			!strings.HasPrefix(response.Body.String(), "# ") {
			t.Errorf("GET %s = %d %q %.20q, want a served text document that opens with a heading", path, response.Code, response.Header().Get("Content-Type"), response.Body)
		}
		for _, match := range links.FindAllStringSubmatch(response.Body.String(), -1) {
			target, err := url.Parse(match[1])
			if err == nil && target.Host == "" && (strings.HasPrefix(target.Path, "/docs/agents/") || target.Path == "/openapi.json") {
				pending = append(pending, target.Path)
			}
		}
	}
	if len(visited) < 2 {
		t.Errorf("visited %v, want llms.txt and the guides it links", visited)
	}
}

func TestAgentDocumentationServesOnlyFiles(t *testing.T) {
	handler := documentation(fstest.MapFS{
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
