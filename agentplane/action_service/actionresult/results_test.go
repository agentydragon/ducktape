package actionresult

import (
	"encoding/json"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/protobuf/types/known/structpb"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
)

const testRequestID = "00000000-0000-0000-0000-000000000001"

func TestMCPResultIsRelayed(t *testing.T) {
	stored, err := structpb.NewValue(map[string]any{
		"content":           []any{map[string]any{"type": "text", "text": "completed"}},
		"structuredContent": map[string]any{"answer": 42},
		"isError":           true,
	})
	if err != nil {
		t.Fatal(err)
	}
	request := &pb.ActionRequest{
		Id:    testRequestID,
		State: pb.ActionState_ACTION_STATE_SUCCEEDED,
		Execution: &pb.Execution{
			Result: stored,
		},
	}

	result, err := Result(request, pb.ExecutorKind_EXECUTOR_KIND_MCP, 0)
	if err != nil {
		t.Fatal(err)
	}
	if !result.IsError {
		t.Fatal("result is not marked as an error")
	}
	if len(result.Content) != 1 {
		t.Fatalf("content has %d blocks, want one", len(result.Content))
	}
	text, ok := result.Content[0].(*mcp.TextContent)
	if !ok || text.Text != "completed" {
		t.Fatalf("content = %#v, want the stored text block", result.Content[0])
	}
	structured, err := json.Marshal(result.StructuredContent)
	if err != nil {
		t.Fatal(err)
	}
	if string(structured) != `{"answer":42}` {
		t.Fatalf("structured content = %s, want {\"answer\":42}", structured)
	}
}

func TestFailedResultRetainsExecutionErrorProtoJSON(t *testing.T) {
	request := &pb.ActionRequest{
		Id:    testRequestID,
		State: pb.ActionState_ACTION_STATE_FAILED,
		Execution: &pb.Execution{
			Error: &pb.ExecutionError{
				Kind:    pb.ExecutionErrorKind_EXECUTION_ERROR_KIND_MCP_UNAVAILABLE,
				Message: "server unavailable",
				Detail: &pb.ExecutionError_McpUnavailable{McpUnavailable: &pb.McpUnavailable{
					Diagnostics: &pb.McpRequestDiagnostics{
						ConnectionAgeSeconds:   1.25,
						RequestDurationSeconds: 2.5,
					},
				}},
			},
		},
	}

	result, err := Result(request, pb.ExecutorKind_EXECUTOR_KIND_MCP, 0)
	if err != nil {
		t.Fatal(err)
	}
	if !result.IsError {
		t.Fatal("failed result is not marked as an error")
	}
	structured, ok := result.StructuredContent.(json.RawMessage)
	if !ok {
		t.Fatalf("structured content type = %T, want json.RawMessage", result.StructuredContent)
	}
	var state struct {
		RequestID string          `json:"request_id"`
		State     string          `json:"state"`
		Error     json.RawMessage `json:"error"`
	}
	if err := json.Unmarshal(structured, &state); err != nil {
		t.Fatal(err)
	}
	const wantError = `{"kind":"EXECUTION_ERROR_KIND_MCP_UNAVAILABLE","message":"server unavailable","mcpUnavailable":{"diagnostics":{"connectionAgeSeconds":1.25,"requestDurationSeconds":2.5}}}`
	if state.RequestID != testRequestID || state.State != "failed" || string(state.Error) != wantError {
		t.Fatalf("structured state = %+v, want request_id %q, state failed, error %s", state, testRequestID, wantError)
	}
	text, ok := result.Content[0].(*mcp.TextContent)
	if !ok || text.Text != "Action request "+testRequestID+" failed without a result: "+wantError {
		t.Fatalf("text fallback = %#v, want the typed execution error", result.Content[0])
	}
}
