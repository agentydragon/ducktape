package mcpserver

import (
	"encoding/json"
	"fmt"
	"strconv"

	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
)

// structuredResult answers a view as structured content, with the same JSON as its one text block.
func structuredResult(view any) (*mcp.CallToolResult, error) {
	text, err := json.Marshal(view)
	if err != nil {
		return nil, err
	}
	return &mcp.CallToolResult{
		Content:           []mcp.Content{&mcp.TextContent{Text: string(text)}},
		StructuredContent: json.RawMessage(text),
	}, nil
}

// requestState is the structured content of an answer that is not the Action's result.
type requestState struct {
	RequestID uuid.UUID `json:"request_id"`
	State     string    `json:"state"`
}

func textResult(text string, structured requestState, isError bool) (*mcp.CallToolResult, error) {
	content, err := json.Marshal(structured)
	if err != nil {
		return nil, err
	}
	return &mcp.CallToolResult{
		Content:           []mcp.Content{&mcp.TextContent{Text: text}},
		StructuredContent: json.RawMessage(content),
		IsError:           isError,
	}, nil
}

// actionResult is a request's outcome as the tool it ran would have answered: an MCP group's stored CallToolResult
// relayed, a Sandbox or orchestration group's result object as structured content, and otherwise a text saying what it waits on or
// why it has no result, as an error where it never will.
func actionResult(request *pb.ActionRequest, executor pb.ExecutorKind, maxWaitSeconds float64) (*mcp.CallToolResult, error) {
	d := &viewDecoder{}
	stateView := requestState{RequestID: d.uuid(request.GetId()), State: enum(d, request.GetState(), "ACTION_STATE_")}
	if d.err != nil {
		return nil, d.err
	}
	id := stateView.RequestID
	prefix := fmt.Sprintf("Action request %s ", id)
	execution, decision := request.GetExecution(), request.GetDecision()
	switch request.GetState() {
	case pb.ActionState_ACTION_STATE_SUCCEEDED:
		if execution.GetResult() == nil {
			return nil, fmt.Errorf("core answered succeeded request %s without a result", id)
		}
		return succeededResult(execution.GetResult(), executor)
	case pb.ActionState_ACTION_STATE_DECISION_PENDING:
		return textResult(prefix+"is waiting for the operator's decision; nothing has run yet."+keepWaiting(id, maxWaitSeconds), stateView, false)
	case pb.ActionState_ACTION_STATE_ALLOWED, pb.ActionState_ACTION_STATE_DISPATCHING, pb.ActionState_ACTION_STATE_RUNNING:
		return textResult(prefix+"was approved and has not finished ("+stateView.State+")."+keepWaiting(id, maxWaitSeconds), stateView, false)
	case pb.ActionState_ACTION_STATE_FAILED, pb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN:
		if execution == nil {
			return nil, fmt.Errorf("core answered %s request %s without an Execution", stateView.State, id)
		}
		failure, err := json.Marshal(execution.GetError())
		if err != nil {
			return nil, err
		}
		if request.GetState() == pb.ActionState_ACTION_STATE_FAILED {
			return textResult(prefix+"failed without a result: "+string(failure), stateView, true)
		}
		return textResult(prefix+"has an unknown outcome: it may or may not have run, and it is never retried. Check "+
			"its effects before asking for it again. "+string(failure), stateView, true)
	case pb.ActionState_ACTION_STATE_DENIED:
		if decision == nil {
			return nil, fmt.Errorf("core answered denied request %s without a Decision", id)
		}
		text := prefix + "was denied and did not run."
		reason := decision.GetDecisionNote()
		if reason == "" {
			reason = decision.GetReasonDescription()
		}
		if reason != "" {
			text += " Reason: " + reason
		}
		return textResult(text, stateView, true)
	case pb.ActionState_ACTION_STATE_CANCELLED:
		return textResult(prefix+"was cancelled before it ran.", stateView, true)
	default:
		return nil, fmt.Errorf("core answered state %v, outside the contract", request.GetState())
	}
}

func keepWaiting(id uuid.UUID, maxWaitSeconds float64) string {
	return fmt.Sprintf(` This is not its result: call get_action_result(request_id=%q, wait={"wait_seconds": %s}) to keep `+
		"waiting.", id, seconds(maxWaitSeconds))
}

func seconds(value float64) string {
	return strconv.FormatFloat(value, 'g', -1, 64)
}

func succeededResult(stored *structpb.Value, executor pb.ExecutorKind) (*mcp.CallToolResult, error) {
	switch executor {
	case pb.ExecutorKind_EXECUTOR_KIND_MCP:
		document, err := protojson.Marshal(stored)
		if err != nil {
			return nil, fmt.Errorf("stored MCP result: %w", err)
		}
		var result mcp.CallToolResult
		if err := json.Unmarshal(document, &result); err != nil {
			return nil, fmt.Errorf("stored MCP result: %w", err)
		}
		return &result, nil
	case pb.ExecutorKind_EXECUTOR_KIND_POD, pb.ExecutorKind_EXECUTOR_KIND_ORCHESTRATION_V1:
		fields := stored.GetStructValue()
		if fields == nil {
			return nil, fmt.Errorf("stored %v result is not an object: %v", executor, stored)
		}
		return structuredResult(fields)
	default:
		return nil, fmt.Errorf("core answered executor kind %v, outside the contract", executor)
	}
}
