package mcpserver

import (
	"encoding/json"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
)

// structuredResult answers an object the way FastMCP answers a tool's structured content: the object, and the same
// object as one JSON text block.
func structuredResult(object jsonObject) *mcp.CallToolResult {
	text := pydanticJSON.encode(object)
	return &mcp.CallToolResult{
		Content:           []mcp.Content{&mcp.TextContent{Text: text}},
		StructuredContent: json.RawMessage(text),
	}
}

func textResult(text string, structured jsonObject, isError bool) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		Content:           []mcp.Content{&mcp.TextContent{Text: text}},
		StructuredContent: json.RawMessage(pydanticJSON.encode(structured)),
		IsError:           isError,
	}
}

// actionResult is a request's outcome as the tool it ran would have answered: an MCP group's stored CallToolResult
// relayed, a Sandbox group's result object as structured content, and otherwise a text saying what it waits on or
// why it has no result, as an error where it never will.
func actionResult(request *pb.ActionRequest, executor pb.ExecutorKind, maxWaitSeconds float64) (*mcp.CallToolResult, error) {
	d := &viewDecoder{}
	id := d.uuid(request.GetId())
	state := enum(d, request.GetState(), "ACTION_STATE_")
	if d.err != nil {
		return nil, d.err
	}
	stateView := jsonObject{{"request_id", id}, {"state", state}}
	prefix := "Action request " + id + " "
	execution, decision := request.GetExecution(), request.GetDecision()
	switch request.GetState() {
	case pb.ActionState_ACTION_STATE_SUCCEEDED:
		if execution.GetResult() == nil {
			return nil, fmt.Errorf("core answered succeeded request %s without a result", id)
		}
		return succeededResult(execution.GetResult(), executor)
	case pb.ActionState_ACTION_STATE_DECISION_PENDING:
		return textResult(prefix+"is waiting for the operator's decision; nothing has run yet."+keepWaiting(id, maxWaitSeconds), stateView, false), nil
	case pb.ActionState_ACTION_STATE_ALLOWED, pb.ActionState_ACTION_STATE_DISPATCHING, pb.ActionState_ACTION_STATE_RUNNING:
		return textResult(prefix+"was approved and has not finished ("+state+")."+keepWaiting(id, maxWaitSeconds), stateView, false), nil
	case pb.ActionState_ACTION_STATE_FAILED, pb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN:
		if execution == nil {
			return nil, fmt.Errorf("core answered %s request %s without an Execution", state, id)
		}
		failure := pythonDumps.encode(nil)
		if execution.GetError() != nil {
			failure = pythonDumps.encode(execution.GetError())
		}
		if request.GetState() == pb.ActionState_ACTION_STATE_FAILED {
			return textResult(prefix+"failed without a result: "+failure, stateView, true), nil
		}
		return textResult(prefix+"has an unknown outcome: it may or may not have run, and it is never retried. Check "+
			"its effects before asking for it again. "+failure, stateView, true), nil
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
		return textResult(text, stateView, true), nil
	case pb.ActionState_ACTION_STATE_CANCELLED:
		return textResult(prefix+"was cancelled before it ran.", stateView, true), nil
	default:
		return nil, fmt.Errorf("core answered state %v, outside the contract", request.GetState())
	}
}

func keepWaiting(id string, maxWaitSeconds float64) string {
	return fmt.Sprintf(" This is not its result: call get_action_result(request_id=%s, wait={'wait_seconds': %s}) to keep "+
		"waiting.", pythonRepr(id), pythonG(maxWaitSeconds))
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
	case pb.ExecutorKind_EXECUTOR_KIND_POD:
		object, ok := freeForm(stored).(jsonObject)
		if !ok {
			return nil, fmt.Errorf("stored Sandbox result is not an object: %v", stored)
		}
		return structuredResult(object), nil
	default:
		return nil, fmt.Errorf("core answered executor kind %v, outside the contract", executor)
	}
}
