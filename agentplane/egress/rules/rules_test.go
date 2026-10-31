package rules

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/agentydragon/ducktape/agentplane/egress/docs"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/agentdocs"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	hopAudience = "test-egress"
	namespace   = "test-workloads"
	description = "a token for the bot account, which can write to its own repositories"
	secretValue = "the-real-credential"
)

var (
	kube *kubeapiserver.Server
	// sbToken, otherToken and nobodyToken prove the ServiceAccounts sb, other and nobody in namespace.
	sbToken, otherToken, nobodyToken string
	unreachable                      = kubeapiserver.UnreachableReviews{Err: errors.New("test API server unreachable")}
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) (err error) {
		kube = server
		for name, token := range map[string]*string{"sb": &sbToken, "other": &otherToken, "nobody": &nobodyToken} {
			account := auth.Account{Namespace: namespace, Name: name}
			if *token, err = server.WorkloadToken(context.Background(), account, hopAudience); err != nil {
				return err
			}
		}
		return nil
	}))
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

// serviceOver is the rules service over index, reviewing bearers with reviews.
func serviceOver(t *testing.T, reviews auth.TokenReviews, index *policy.Index) *Service {
	t.Helper()
	hop, err := auth.NewResolver(reviews, hopAudience, []string{namespace})
	if err != nil {
		t.Fatal(err)
	}
	return NewService(identity.NewVerifier(hop, nil), index)
}

// api is the rules API over index, with no documentation: its facade forwards to a gRPC server over
// loopback, as in the proxy.
func api(t *testing.T, reviews auth.TokenReviews, index *policy.Index) http.Handler {
	t.Helper()
	service := serviceOver(t, reviews, index)
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
	rulesFacade, err := facade.NewHandler(ctx, listener.Addr().String(), 0, egressrulespb.RegisterEgressRulesHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	return New(rulesFacade, fstest.MapFS{}, docs.OpenAPI)
}

// documentation is the rules API serving agentDocs, with no rules behind it.
func documentation(agentDocs fs.FS) http.Handler {
	return New(http.NotFoundHandler(), agentDocs, docs.OpenAPI)
}

// githubIndex grants the sandbox account `sb` the GitHub policy and `other` a policy of its own.
func githubIndex() *policy.Index {
	index := policy.NewIndex()
	index.Rules.PutPolicySet(policySet("github", githubRule, openRule))
	index.Rules.PutPolicySet(policySet("other-only", openRule))
	index.Rules.PutBinding(binding("sb", "b-sb", "github"))
	index.Rules.PutBinding(binding("other", "b-other", "other-only"))
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
	handler := api(t, kube.TokenReviews(), githubIndex())

	response := get(handler, "/v1/rules", bearer(sbToken)...)

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
	for _, forbidden := range []string{secretValue, "vault-entry", "credential-key", "secretRef", sbToken} {
		if strings.Contains(response.Body.String(), forbidden) {
			t.Errorf("%q reached a sandbox-readable response", forbidden)
		}
	}
}

func TestFacadeAnswersNoEgressWithAnEmptyList(t *testing.T) {
	handler := api(t, kube.TokenReviews(), githubIndex())

	response := get(handler, "/v1/rules", bearer(nobodyToken)...)

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
		wantReviews int32
	}{
		"no Authorization":   {nil, 0},
		"not a bearer":       {[]string{"Authorization", "Basic abc"}, 0},
		"two Authorizations": {[]string{"Authorization", "Bearer " + sbToken, "Authorization", "Bearer " + sbToken}, 0},
		"a forged bearer":    {bearer("forged"), 1},
		"only a hop bearer":  {[]string{"Proxy-Authorization", "Bearer " + sbToken}, 0},
	} {
		t.Run(name, func(t *testing.T) {
			reviewed := &kubeapiserver.CountedReviews{TokenReviews: kube.TokenReviews()}
			handler := api(t, reviewed, githubIndex())

			response := get(handler, "/v1/rules", c.headers...)

			if code := at(t, document(t, response), "code"); response.Code != http.StatusUnauthorized || code != float64(codes.Unauthenticated) {
				t.Errorf("status %d, code %v; want 401 and Unauthenticated: %s", response.Code, code, response.Body)
			}
			if got := reviewed.Calls.Load(); got != c.wantReviews {
				t.Errorf("%d token reviews, want %d: an unusable header must not reach the API server", got, c.wantReviews)
			}
			if strings.Contains(response.Body.String(), "forged") {
				t.Errorf("the response echoes the rejected token: %s", response.Body)
			}
		})
	}
}

func TestFacadeIsUnavailableWhenTheBearerCannotBeReviewed(t *testing.T) {
	handler := api(t, unreachable, githubIndex())

	response := get(handler, "/v1/rules", bearer(sbToken)...)

	if code := at(t, document(t, response), "code"); response.Code != http.StatusServiceUnavailable || code != float64(codes.Unavailable) {
		t.Errorf("status %d, code %v; want 503 and Unavailable: an unanswered review says nothing about the caller", response.Code, code)
	}
}

func TestFacadeCannotBeAimedAtAnotherSubject(t *testing.T) {
	handler := api(t, kube.TokenReviews(), githubIndex())
	request := httptest.NewRequest(http.MethodGet, "/v1/rules?namespace="+namespace+"&name=other", nil)
	request.Header.Set("Authorization", "Bearer "+sbToken)
	request.Header.Set("Proxy-Authorization", "Bearer "+otherToken)
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
	handler := api(t, kube.TokenReviews(), githubIndex())

	for _, path := range []string{"/", "/decisions", "/healthz", "/v1/decisions", "/docs/agents/"} {
		if response := get(handler, path, bearer(sbToken)...); response.Code != http.StatusNotFound {
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

func TestAgentDocumentationGraphIsServed(t *testing.T) {
	agentDocs, err := fs.Sub(docs.Agents, "agents")
	if err != nil {
		t.Fatal(err)
	}
	agentdocs.AssertServed(t, documentation(agentDocs))
}
