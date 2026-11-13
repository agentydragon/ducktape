package mcpserver

import (
	"context"
	"encoding/json"
	"errors"
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

var errSDKRejectedDirectToolMetadata = errors.New("SDK rejected direct tool metadata")

// directTools serves each group's reviewed direct tools as MCP tools of their own, named <group>__<action>.
// Every currently offered direct tool is registered with the SDK so its normal dispatch and HTTP header validation
// apply. tools/list hides the ones no policy of the caller's could auto-approve. The listing is presentation only: the
// core decides every call again, and a call no policy approves is refused before anything is stored.
func (t *tools) registerDirectTools(server *mcp.Server, catalog *pb.ListActionGroupsResponse,
	logger *slog.Logger,
) (map[string]actionIdentity, error) {
	direct := map[string]actionIdentity{}
	groups := slices.SortedFunc(slices.Values(catalog.GetActionGroups()), func(a, b *pb.ActionGroup) int {
		return strings.Compare(a.GetKey(), b.GetKey())
	})
	for _, group := range groups {
		for _, name := range slices.Sorted(slices.Values(group.GetDirectTools())) {
			action := offered(group, name)
			if action == nil {
				continue
			}
			identity := actionIdentity{Group: group.GetKey(), Name: name}
			tool := t.directTool(group, action)
			if err := addDirectTool(server, tool, func(ctx context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
				return t.handleDirectTool(ctx, logger, identity, request)
			}); err != nil {
				return nil, fmt.Errorf("register direct tool %q: %w", tool.Name, err)
			}
			direct[tool.Name] = identity
		}
	}
	return direct, nil
}

// addDirectTool converts the SDK's metadata validation panic into a registration error at the boundary where catalog
// metadata enters its registry. Keep the recover scoped to AddTool so unrelated panics still fail normally, and let the
// SDK remain the sole schema validator.
func addDirectTool(server *mcp.Server, tool *mcp.Tool, handler mcp.ToolHandler) (err error) {
	defer func() {
		if recover() != nil {
			err = errSDKRejectedDirectToolMetadata
		}
	}()
	server.AddTool(tool, handler)
	return nil
}

// directTools filters the SDK's paginated tool listing to the caller's policy. Every configured tool remains registered
// so a call the policy does not auto-approve still reaches the core's decided-only admission check.
func (t *tools) directTools(logger *slog.Logger, direct map[string]actionIdentity) mcp.Middleware {
	return func(next mcp.MethodHandler) mcp.MethodHandler {
		return func(ctx context.Context, method string, request mcp.Request) (mcp.Result, error) {
			listRequest, ok := request.(*mcp.ListToolsRequest)
			if !ok || len(direct) == 0 {
				return next(ctx, method, request)
			}
			result, err := next(ctx, method, listRequest)
			if err != nil {
				return nil, err
			}
			listed, ok := result.(*mcp.ListToolsResult)
			if !ok || listed == nil {
				return nil, fmt.Errorf("SDK list-tools handler returned %T, want *mcp.ListToolsResult", result)
			}
			policy, err := t.core.GetActionPolicy(ctx, &pb.GetActionPolicyRequest{})
			if err != nil {
				logger.WarnContext(ctx, "listing auto-approved direct tools failed", "error", err)
				listed.Tools = filterDirectTools(listed.Tools, direct, nil)
				return listed, nil
			}
			approvable := map[actionIdentity]bool{}
			for _, identity := range policy.GetActionPolicy().GetAutoApprovable() {
				approvable[actionIdentity{Group: identity.GetGroup(), Name: identity.GetName()}] = true
			}
			listed.Tools = filterDirectTools(listed.Tools, direct, approvable)
			return listed, nil
		}
	}
}

func filterDirectTools(tools []*mcp.Tool, direct map[string]actionIdentity, approvable map[actionIdentity]bool) []*mcp.Tool {
	return slices.DeleteFunc(tools, func(tool *mcp.Tool) bool {
		identity, isDirect := direct[tool.Name]
		return isDirect && !approvable[identity]
	})
}

func (t *tools) handleDirectTool(ctx context.Context, logger *slog.Logger, identity actionIdentity, request *mcp.CallToolRequest,
) (*mcp.CallToolResult, error) {
	// A submission finishes after its caller leaves; the wait still observes the transport context.
	ctx = context.WithoutCancel(ctx)
	name := request.Params.Name
	groups, err := t.core.ListActionGroups(ctx, &pb.ListActionGroupsRequest{})
	if err != nil {
		return failedResult(ctx, logger, name, err), nil
	}
	var group *pb.ActionGroup
	for _, candidate := range groups.GetActionGroups() {
		if candidate.GetKey() == identity.Group {
			group = candidate
			break
		}
	}
	if offered(group, identity.Name) == nil {
		return nil, &jsonrpc.Error{Code: jsonrpc.CodeInvalidParams, Message: fmt.Sprintf("unknown tool %q", name)}
	}
	var arguments map[string]any
	if len(request.Params.Arguments) > 0 {
		if err := json.Unmarshal(request.Params.Arguments, &arguments); err != nil {
			return nil, &jsonrpc.Error{Code: jsonrpc.CodeInvalidParams, Message: "arguments must be an object"}
		}
	}
	result, err := t.callDirect(ctx, group, identity, arguments)
	if err != nil {
		return failedResult(ctx, logger, name, err), nil
	}
	return result, nil
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
