package session

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/actionresult"
	pb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"
)

func (r *CodeMode) hostTools(ctx context.Context) ([]protocol.CodeToolDefinition, error) {
	var definitions []protocol.CodeToolDefinition
	if r.executor != nil {
		var err error
		definitions, err = toolcontract.HostToolDefinitions(toolcontract.Definitions())
		if err != nil {
			return nil, err
		}
	}
	if r.actions != nil {
		groups, err := r.actions.Catalog(ctx)
		if err != nil {
			return nil, err
		}
		for _, group := range groups {
			for _, action := range group.GetActions() {
				definition, err := hostActionDefinition(group.GetKey(), action)
				if err != nil {
					return nil, err
				}
				definitions = append(definitions, definition)
			}
		}
	}
	names := make(map[string]bool, len(definitions))
	for _, definition := range definitions {
		if names[definition.Name] {
			return nil, fmt.Errorf("code-mode catalog has duplicate tool identifier %q", definition.Name)
		}
		names[definition.Name] = true
	}
	slices.SortFunc(definitions, func(a, b protocol.CodeToolDefinition) int { return strings.Compare(a.Name, b.Name) })
	return definitions, nil
}

// hostActionDefinition makes the absence of Agentplane's tool-schema validation visible in ALL_TOOLS.
func hostActionDefinition(namespace string, action *pb.Action) (protocol.CodeToolDefinition, error) {
	schema, err := protojson.Marshal(action.GetInputSchema())
	if err != nil {
		return protocol.CodeToolDefinition{}, err
	}
	description := action.GetDescription()
	if action.GetInputSchemaValidation().GetStatus() == pb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_NOT_ENFORCED {
		description += "\n\nWarning: Agentplane does not validate arguments against this tool's input schema."
	}
	return toolcontract.HostActionDefinition(namespace+"__"+action.GetName(), protocol.ToolName{Name: action.GetName(), Namespace: &namespace}, description, schema)
}

func (r *CodeMode) invokeAction(ctx context.Context, intent string, invocation codemode.ToolInvocation) (codemode.DelegateResult, codemode.DelegateResult, error) {
	var empty codemode.DelegateResult
	reject := func(message string) (codemode.DelegateResult, codemode.DelegateResult, error) {
		if err := r.store.FinishNested(ctx, r.owner, intent, map[string]any{"error": message}); err != nil {
			return empty, empty, err
		}
		reply := codemode.DelegateResult{Status: "error", Message: message}
		return reply, reply, nil
	}
	if r.actions == nil {
		return reject("Actions are not configured")
	}
	groups, err := r.actions.Catalog(ctx)
	if err != nil {
		return reject("Action discovery failed: " + err.Error())
	}
	kind := pb.ExecutorKind_EXECUTOR_KIND_UNSPECIFIED
	for _, group := range groups {
		if group.GetKey() == *invocation.ToolName.Namespace {
			for _, action := range group.GetActions() {
				if action.GetName() == invocation.ToolName.Name {
					kind = group.GetExecutorKind()
				}
			}
		}
	}
	if kind == pb.ExecutorKind_EXECUTOR_KIND_UNSPECIFIED {
		return reject("Action is not in the current catalog")
	}
	arguments := new(structpb.Struct)
	if err := protojson.Unmarshal(invocation.Input, arguments); err != nil {
		return reject("Action arguments must be a JSON object: " + err.Error())
	}
	// RecordNested committed this key before any submission. A lost response is recovered
	// by that exact key; neither approval waits nor transport failures submit another Action.
	title := []rune("Run " + *invocation.ToolName.Namespace + "/" + invocation.ToolName.Name)
	if len(title) > 60 {
		title = title[:60]
	}
	receipt, submitErr := r.actions.Submit(ctx, &pb.SubmitActionRequestRequest{
		IdempotencyKey: intent, Action: &pb.ActionIdentity{Group: *invocation.ToolName.Namespace, Name: invocation.ToolName.Name},
		Arguments: arguments, Title: string(title),
	})
	if submitErr != nil {
		receipt, err = r.actions.Recover(ctx, intent)
		if err != nil {
			if status.Code(err) == codes.NotFound {
				switch status.Code(submitErr) {
				case codes.InvalidArgument, codes.Unauthenticated, codes.PermissionDenied, codes.NotFound, codes.FailedPrecondition:
					return reject(submitErr.Error())
				}
			}
			return empty, empty, &protocol.UnknownEffectError{Reason: fmt.Sprintf("Action submission under idempotency key %s has no confirmed receipt: %v", intent, submitErr)}
		}
	}
	if err := r.recordActionReceipt(ctx, intent, receipt); err != nil {
		return empty, empty, err
	}
	receipt, err = r.actions.Await(ctx, receipt.GetId())
	if err != nil {
		return empty, empty, err
	}
	if err := r.recordActionReceipt(ctx, intent, receipt); err != nil {
		return empty, empty, err
	}
	result, err := actionresult.Result(receipt, kind, 0)
	if err != nil {
		return empty, empty, err
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return empty, empty, err
	}
	if err := r.store.FinishNested(ctx, r.owner, intent, json.RawMessage(encoded)); err != nil {
		return empty, empty, err
	}
	reply := codemode.DelegateResult{Status: "ok", Value: &codemode.ToolResult{Type: "tool/result", Result: encoded}}
	return reply, reply, nil
}

func (r *CodeMode) recordActionReceipt(ctx context.Context, intent string, receipt *pb.ActionRequest) error {
	encoded, err := protojson.Marshal(receipt)
	if err != nil {
		return err
	}
	return r.store.ProtocolEvent(ctx, r.owner, intent, eventpb.ExecutionProgress_KIND_TOOL, eventpb.ExecutionProgress_PHASE_DISPATCH,
		map[string]any{"idempotency_key": intent, "action_receipt": json.RawMessage(encoded)})
}
