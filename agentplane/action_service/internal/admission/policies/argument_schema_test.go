package policies

import (
	"context"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/santhosh-tekuri/jsonschema/v6"
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
	} else if _, ok := outcome.(NotMatched); !ok {
		t.Fatalf("timed-out policy must go to human path, got %T", outcome)
	}
	if outcome := policy.evaluate(context.Background(), action, map[string]any{"value": "safe"}, nil); outcome == nil {
		t.Fatal("policy returned nil")
	} else if _, ok := outcome.(Matched); !ok {
		t.Fatalf("normal policy should match, got %T", outcome)
	}
}
