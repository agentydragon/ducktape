package policies

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"
	"k8s.io/apimachinery/pkg/util/sets"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
)

// golden.json contains the policy package's Go-owned parsing and evaluation contract vectors.
//
//go:embed golden.json
var goldenJSON []byte

type golden struct {
	Rules map[string]struct {
		Rule    policyapi.Rule `json:"rule"`
		Outcome string         `json:"outcome"`
	} `json:"rules"`
	Evaluations map[string]struct {
		Rule       policyapi.Rule       `json:"rule"`
		Action     store.ActionIdentity `json:"action"`
		Arguments  json.RawMessage      `json:"arguments"`
		Visibility visibilityVector     `json:"visibility"`
		Outcome    evaluationOutcome    `json:"outcome"`
	} `json:"evaluations"`
	Decisions map[string]struct {
		Namespace string `json:"namespace"`
		Bindings  []struct {
			Name            string `json:"name"`
			ResourceVersion string `json:"resource_version"`
			PolicySets      []struct {
				Name       string           `json:"name"`
				Generation int64            `json:"generation"`
				Rules      []policyapi.Rule `json:"rules"`
			} `json:"policy_sets"`
		} `json:"bindings"`
		Action     store.ActionIdentity `json:"action"`
		Arguments  json.RawMessage      `json:"arguments"`
		Visibility visibilityVector     `json:"visibility"`
		Outcome    decisionOutcome      `json:"outcome"`
	} `json:"decisions"`
}

type visibilityVector struct {
	Public      [][2]string `json:"public"`
	Unavailable bool        `json:"unavailable"`
}

type evaluationOutcome struct {
	Matched    *matchedOutcome `json:"matched"`
	NotMatched *string         `json:"not_matched"`
	Raised     *string         `json:"raised"`
}

type matchedOutcome struct {
	Explanation string                   `json:"explanation"`
	Repository  *store.MatchedRepository `json:"repository"`
}

type decisionOutcome struct {
	Verdict           string                `json:"verdict"`
	ReasonCode        string                `json:"reason_code"`
	ReasonDescription *string               `json:"reason_description"`
	Evidence          *store.PolicyEvidence `json:"evidence"`
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	var vectors golden
	if err := json.Unmarshal(goldenJSON, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

func TestRulesParseAccordingToFixture(t *testing.T) {
	vectors := loadGolden(t).Rules
	for name, vector := range vectors {
		t.Run(name, func(t *testing.T) {
			got := "accepted"
			if _, err := ParseRule(vector.Rule); err != nil {
				got = "refused"
			}
			if got != vector.Outcome {
				t.Errorf("fixture says %s; Go got %s", vector.Outcome, got)
			}
		})
	}
}

func TestPoliciesEvaluateAccordingToFixture(t *testing.T) {
	vectors := loadGolden(t).Evaluations
	for name, vector := range vectors {
		t.Run(name, func(t *testing.T) {
			var got evaluationOutcome
			if policy, err := ParseRule(vector.Rule); err != nil {
				refused := "refused: " + err.Error()
				got.Raised = &refused
			} else {
				got = outcomeOf(policy.evaluate(t.Context(), vector.Action, arguments(t, vector.Arguments), fakeGitHub(t, vector.Visibility)))
			}
			want := vector.Outcome
			if vector.Rule.Policy.Type == string(store.PolicyArgumentSchema) {
				// Validator prose can change between versions; the fixture still requires the same outcome kind.
				want.NotMatched, got.NotMatched = nonEmpty(want.NotMatched), nonEmpty(got.NotMatched)
				want.Raised, got.Raised = nonEmpty(want.Raised), nonEmpty(got.Raised)
			}
			if !reflect.DeepEqual(want, got) {
				t.Errorf("fixture says\n%s\nGo got\n%s", asJSON(t, want), asJSON(t, got))
			}
		})
	}
}

func TestProviderDecidesAccordingToFixture(t *testing.T) {
	for name, vector := range loadGolden(t).Decisions {
		t.Run(name, func(t *testing.T) {
			var bindings []Binding
			for _, binding := range vector.Bindings {
				resolved := Binding{Namespace: vector.Namespace, Name: binding.Name, ResourceVersion: binding.ResourceVersion}
				for _, set := range binding.PolicySets {
					parsed := PolicySet{Namespace: vector.Namespace, Name: set.Name, Generation: set.Generation}
					for number, rule := range set.Rules {
						if rule.Type != RuleType {
							continue
						}
						policy, err := ParseRule(rule)
						if err != nil {
							t.Fatalf("rules[%d]: %v", number, err)
						}
						parsed.Policies = append(parsed.Policies, reader.NumberedRule[Policy]{Number: number, Rule: policy})
					}
					resolved.PolicySets = append(resolved.PolicySets, parsed)
				}
				bindings = append(bindings, resolved)
			}
			provider := NewProvider(fakeGitHub(t, vector.Visibility))
			var got decisionOutcome
			switch decision := provider.Decide(t.Context(), bindings, vector.Action, arguments(t, vector.Arguments)).(type) {
			case Allow:
				if decision.Vote.Provider != ProviderName {
					t.Errorf("provider %q", decision.Vote.Provider)
				}
				got = decisionOutcome{
					Verdict:           string(decision.Vote.Verdict),
					ReasonCode:        decision.Vote.ReasonCode,
					ReasonDescription: decision.Vote.ReasonDescription,
					Evidence:          decision.Vote.Evidence,
				}
				if err := decision.Vote.Validate(); err != nil {
					t.Errorf("the store refuses the vote: %v", err)
				}
			case NoOpinion:
				// Python's reason code for no opinion; nothing records one.
				got = decisionOutcome{Verdict: "no_opinion", ReasonCode: "no_auto_approve_match", ReasonDescription: &decision.ReasonDescription}
			}
			if !reflect.DeepEqual(got, vector.Outcome) {
				t.Errorf("fixture says\n%s\nGo got\n%s", asJSON(t, vector.Outcome), asJSON(t, got))
			}
		})
	}
}

func outcomeOf(outcome Outcome) evaluationOutcome {
	var got evaluationOutcome
	switch outcome := outcome.(type) {
	case Matched:
		got.Matched = &matchedOutcome{Explanation: outcome.Explanation, Repository: outcome.Repository}
	case NotMatched:
		got.NotMatched = &outcome.Reason
	}
	return got
}

func nonEmpty(reason *string) *string {
	if reason == nil {
		return nil
	}
	present := "(a reason)"
	return &present
}

func arguments(t *testing.T, raw json.RawMessage) map[string]any {
	t.Helper()
	decoded, err := jsonschema.UnmarshalJSON(bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	return decoded.(map[string]any)
}

func asJSON(t *testing.T, value any) string {
	t.Helper()
	text, err := json.MarshalIndent(value, "", " ")
	if err != nil {
		t.Fatal(err)
	}
	return string(text)
}

// fakeGitHub stands in for GitHub's unauthenticated repository endpoint, confirming only named fixture repositories.
func fakeGitHub(t *testing.T, visibility visibilityVector) *Visibility {
	t.Helper()
	confirmed := sets.New[[2]string]()
	for _, pair := range visibility.Public {
		confirmed.Insert([2]string{fold(pair[0]), fold(pair[1])})
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if visibility.Unavailable {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		_, path, _ := strings.Cut(r.URL.EscapedPath(), "/repos/")
		escapedOwner, escapedRepository, _ := strings.Cut(path, "/")
		owner, ownerErr := url.PathUnescape(escapedOwner)
		repository, repositoryErr := url.PathUnescape(escapedRepository)
		if ownerErr != nil || repositoryErr != nil {
			t.Errorf("path %s: %v %v", r.URL.EscapedPath(), ownerErr, repositoryErr)
		}
		if !confirmed.Has([2]string{fold(owner), fold(repository)}) {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if _, err := w.Write([]byte(`{"private": false}`)); err != nil {
			t.Error(err)
		}
	}))
	t.Cleanup(server.Close)
	visibilityService, err := NewVisibility(server.URL, VisibilityTTL)
	if err != nil {
		t.Fatal(err)
	}
	return visibilityService
}
