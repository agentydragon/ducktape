package mcpserver

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"slices"
	"strings"

	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/structpb"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
)

// directSeparator joins a direct tool's group and Action. A group that lists direct tools has none in its key, so a
// name splits at the first one.
const directSeparator = "__"

// directCallTitle is what an operator reading the history sees for a direct call, which no caller titled.
const directCallTitle = "Direct tool call"

// directTools serves each group's reviewed direct tools as MCP tools of their own, named <group>__<action>.
// tools/list adds the ones some policy of the caller's could auto-approve. tools/call runs any configured one as an
// Action submitted decided-only, so a call no policy approves is refused before anything is stored. The listing is
// presentation only: the core decides every call again. Other requests pass through.
func (t *tools) directTools(logger *slog.Logger) mcp.Middleware {
	return func(next mcp.MethodHandler) mcp.MethodHandler {
		return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
			switch request := request.(type) {
			case *mcp.ListToolsRequest:
				result, err := next(ctx, method, request)
				if err != nil {
					return nil, err
				}
				listed := result.(*mcp.ListToolsResult)
				if listed.NextCursor != "" {
					return listed, nil
				}
				direct, err := t.listDirect(ctx)
				if err != nil {
					// As a failing FastMCP provider did: the caller still gets the generic tools.
					logger.WarnContext(ctx, "listing direct tools failed", "error", err)
					return listed, nil
				}
				listed.Tools = append(listed.Tools, direct...)
				return listed, nil
			case *mcp.CallToolRequest:
				groupKey, actionKey, found := strings.Cut(request.Params.Name, directSeparator)
				if !found {
					break
				}
				// Detached like addTool's tools: a submission finishes after its caller left.
				ctx := context.WithoutCancel(ctx)
				groups, err := t.core.ListActionGroups(ctx, &pb.ListActionGroupsRequest{})
				if err != nil {
					return failedResult(ctx, logger, request.Params.Name, err), nil
				}
				var group *pb.ActionGroup
				var action *pb.Action
				for _, candidate := range groups.GetActionGroups() {
					if candidate.GetKey() == groupKey {
						group, action = candidate, offered(candidate, actionKey)
					}
				}
				if action == nil {
					break
				}
				var arguments map[string]any
				if len(request.Params.Arguments) > 0 {
					if err := json.Unmarshal(request.Params.Arguments, &arguments); err != nil {
						return nil, &jsonrpc.Error{Code: jsonrpc.CodeInvalidParams, Message: "arguments must be an object"}
					}
				}
				result, err := t.callDirect(ctx, group, actionIdentity{Group: groupKey, Name: actionKey}, arguments)
				if err != nil {
					return failedResult(ctx, logger, request.Params.Name, err), nil
				}
				return result, nil
			}
			return next(ctx, method, request)
		}
	}
}

// listDirect is the caller's direct tools: each group's, in key order, whose Action the caller's policy lists as
// auto-approvable.
func (t *tools) listDirect(ctx context.Context) ([]*mcp.Tool, error) {
	groups, err := t.core.ListActionGroups(ctx, &pb.ListActionGroupsRequest{})
	if err != nil {
		return nil, err
	}
	policy, err := t.core.GetActionPolicy(ctx, &pb.GetActionPolicyRequest{})
	if err != nil {
		return nil, err
	}
	approvable := map[actionIdentity]bool{}
	for _, identity := range policy.GetActionPolicy().GetAutoApprovable() {
		approvable[actionIdentity{Group: identity.GetGroup(), Name: identity.GetName()}] = true
	}
	sorted := slices.SortedFunc(slices.Values(groups.GetActionGroups()), func(a, b *pb.ActionGroup) int {
		return strings.Compare(a.GetKey(), b.GetKey())
	})
	listed := []*mcp.Tool{}
	for _, group := range sorted {
		for _, name := range slices.Sorted(slices.Values(group.GetDirectTools())) {
			if action := offered(group, name); action != nil && approvable[actionIdentity{Group: group.GetKey(), Name: name}] {
				listed = append(listed, t.directTool(group, action))
			}
		}
	}
	return listed, nil
}

// offered is the Action a group's direct tool name runs: one of its reviewed direct tools that the group offers now.
// An unavailable group lists no Actions, so it offers none.
func offered(group *pb.ActionGroup, name string) *pb.Action {
	if !slices.Contains(group.GetDirectTools(), name) {
		return nil
	}
	for _, action := range group.GetActions() {
		if action.GetName() == name {
			return action
		}
	}
	return nil
}

func (t *tools) directTool(group *pb.ActionGroup, action *pb.Action) *mcp.Tool {
	// Some clients drop a tool without a title; an untitled Action takes its name.
	title := action.GetName()
	if action.Title != nil {
		title = action.GetTitle()
	}
	var schema any = map[string]any{"type": "object"}
	if len(action.GetInputSchema().GetFields()) > 0 {
		schema = action.GetInputSchema()
	}
	var annotations *mcp.ToolAnnotations
	if hints := action.GetAnnotations(); hints != nil {
		annotations = &mcp.ToolAnnotations{
			ReadOnlyHint:    hints.GetReadOnlyHint(),
			DestructiveHint: hints.DestructiveHint,
			IdempotentHint:  hints.GetIdempotentHint(),
			OpenWorldHint:   hints.OpenWorldHint,
		}
	}
	return &mcp.Tool{
		Name:  group.GetKey() + directSeparator + action.GetName(),
		Title: group.GetTitle() + ": " + title,
		Description: action.GetDescription() + "\n\nRuns as an Agentplane Action under your policy: a call it does not " +
			"approve automatically is refused without running, and one still running after " +
			seconds(t.directWaitSeconds) + "s answers with a request id to wait on with get_action_result.",
		InputSchema: schema,
		Annotations: annotations,
	}
}

// callDirect runs action as a request a policy decides at admission, and answers as its tool did once it finishes,
// or with the request to wait on once the direct wait passes.
func (t *tools) callDirect(ctx context.Context, group *pb.ActionGroup, action actionIdentity, arguments map[string]any) (*mcp.CallToolResult, error) {
	fields, err := structpb.NewStruct(arguments)
	if err != nil {
		return nil, fmt.Errorf("arguments: %w", err)
	}
	submitted, err := t.core.SubmitActionRequest(ctx, &pb.SubmitActionRequestRequest{
		IdempotencyKey: "direct-" + uuid.NewString(),
		Action:         &pb.ActionIdentity{Group: action.Group, Name: action.Name},
		Arguments:      fields,
		Title:          directCallTitle,
		DecidedOnly:    true,
	})
	if status.Code(err) == codes.PermissionDenied {
		return undecidedResult(action, status.Convert(err).Message())
	}
	if err != nil {
		return nil, submitError(err)
	}
	request, err := t.read(ctx, submitted.GetActionRequest().GetId(), waitOptions{WaitSeconds: t.directWaitSeconds, WaitUntil: "terminal"})
	if err != nil {
		return nil, err
	}
	return actionResult(request, group.GetExecutorKind(), t.maxWaitSeconds)
}

// undecidedResult refuses a call no policy decides, with the core's reasons and how to ask the operator instead.
func undecidedResult(action actionIdentity, reasons string) (*mcp.CallToolResult, error) {
	identity, err := json.Marshal(action)
	if err != nil {
		return nil, err
	}
	return &mcp.CallToolResult{
		Content: []mcp.Content{&mcp.TextContent{
			Text: "Not run, and nothing was submitted: no policy of yours approves this call automatically (" + reasons +
				"). To ask the operator instead, call request_action with action " + string(identity) +
				", these arguments, a title and a fresh idempotency_key.",
		}},
		IsError: true,
	}, nil
}

// failedResult answers a failed direct call as addTool answers a failed tool.
func failedResult(ctx context.Context, logger *slog.Logger, tool string, err error) *mcp.CallToolResult {
	result := &mcp.CallToolResult{}
	result.SetError(toolFailure(ctx, logger, tool, err))
	return result
}
