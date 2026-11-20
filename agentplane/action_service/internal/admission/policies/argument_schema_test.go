package policies

import (
	"context"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/santhosh-tekuri/jsonschema/v6"
	"k8s.io/apimachinery/pkg/util/sets"
)

func TestPatternTimeoutDoesNotAutoApprove(t *testing.T) {
	// The `not` would turn a timeout treated as a plain mismatch into a match.
	document, err := jsonschema.UnmarshalJSON(strings.NewReader(`{"properties":{"value":{"not":{"pattern":"^(?=a)(a+)+$"}}}}`))
	if err != nil {
		t.Fatal(err)
	}
	schema, err := catalog.CompileSchema(document)
	if err != nil {
		t.Fatal(err)
	}
	policy := ArgumentSchema{compiled: schema}
	action := store.ActionIdentity{Group: "test-group", Name: "test-action"}
	if outcome := policy.evaluate(context.Background(), action, map[string]any{"value": strings.Repeat("a", 96) + "!"}, nil); outcome == nil {
		t.Fatal("policy returned nil")
	} else if _, ok := outcome.(Indeterminate); !ok {
		t.Fatalf("timed-out policy must go to human path, got %T", outcome)
	}
	if outcome := policy.evaluate(context.Background(), action, map[string]any{"value": "safe"}, nil); outcome == nil {
		t.Fatal("policy returned nil")
	} else if _, ok := outcome.(Matched); !ok {
		t.Fatalf("normal policy should match, got %T", outcome)
	}
}

// Even an independent later auto-approval must not hide an earlier schema evaluation failure.
func TestPatternTimeoutStopsLaterAutoApproval(t *testing.T) {
	document, err := jsonschema.UnmarshalJSON(strings.NewReader(`{"properties":{"value":{"not":{"pattern":"^(?=a)(a+)+$"}}}}`))
	if err != nil {
		t.Fatal(err)
	}
	schema, err := catalog.CompileSchema(document)
	if err != nil {
		t.Fatal(err)
	}
	action := store.ActionIdentity{Group: "test-group", Name: "test-action"}
	listed := map[string]sets.Set[string]{action.Group: sets.New(action.Name)}
	bindings := []Binding{{PolicySets: []PolicySet{{Policies: []reader.NumberedRule[Policy]{
		{Number: 0, Rule: Policy{Actions: listed, Kind: ArgumentSchema{compiled: schema}}},
		{Number: 1, Rule: Policy{Actions: listed, Kind: ExactActions{}}},
	}}}}}
	provider := NewProvider(nil)
	slow := map[string]any{"value": strings.Repeat("a", 96) + "!"}
	if got := provider.Decide(t.Context(), bindings, action, slow); got == nil {
		t.Fatal("provider returned nil")
	} else if _, ok := got.(NoOpinion); !ok {
		t.Fatalf("failed evaluation fell through to approval: %T", got)
	}
	// A true mismatch still lets the later independent rule approve.
	if got := provider.Decide(t.Context(), bindings, action, map[string]any{"value": "aaaa"}); got == nil {
		t.Fatal("provider returned nil")
	} else if _, ok := got.(Allow); !ok {
		t.Fatalf("normal mismatch did not allow later rule: %T", got)
	}
}
