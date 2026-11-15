package actions

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestActionTextUsesGeneratedProtoJSON(t *testing.T) {
	group, err := groupLine(json.RawMessage(`{"key":"test-group","title":"Test","executorKind":"EXECUTOR_KIND_MCP","available":true,"actions":[{},{}]}`))
	if err != nil || group != "test-group  mcp  2 actions  Test" {
		t.Fatalf("group text = %q, error %v", group, err)
	}

	action, err := actionDetail(json.RawMessage(`{"group":"test-group","name":"echo","description":"Echo text","inputSchema":{"type":"object","properties":{"message":{"type":"string"}}}}`))
	if err != nil || !strings.Contains(action, "input schema:\n{") || !strings.Contains(action, `"message"`) {
		t.Fatalf("action detail = %q, error %v", action, err)
	}

	policy, err := policyText(json.RawMessage(`{"subject":{"namespace":"test","name":"agent"},"synced":true,"bindings":[{"name":"binding","expiresAt":"2026-01-02T03:04:05Z","policySets":[{"name":"set","generation":"1"}]}],"autoApproveIf":[{"binding":"binding","policySet":"set","source":"POLICY_SOURCE_RULES","index":0,"policy":{"exactActions":{"actions":[{"group":"test-group","names":["echo"]}]}}}]}`))
	wantPolicy := "subject test/agent  synced true\nbinding binding  sets set  expires 2026-01-02T03:04:05Z\nauto-approve set#0  exact_actions  test-group/echo"
	if err != nil || policy != wantPolicy {
		t.Fatalf("policy text = %q, error %v; want %q", policy, err, wantPolicy)
	}

	cancellation, err := cancellationLine(json.RawMessage(`{"outcome":"CANCELLATION_OUTCOME_CANCELLED","actionRequest":{"id":"request-id","action":{"group":"test-group","name":"echo"},"state":"ACTION_STATE_CANCELLED","title":"Stop it"}}`))
	if err != nil || cancellation != `cancelled  request-id  cancelled  test-group/echo  "Stop it"` {
		t.Fatalf("cancellation text = %q, error %v", cancellation, err)
	}
}
