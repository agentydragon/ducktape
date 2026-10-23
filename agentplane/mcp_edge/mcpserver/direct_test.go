package mcpserver

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
)

// directCatalog offers test-mcp/alpha as a direct tool; test-mcp/internal is an Action that is not one.
var directCatalog = &pb.ListActionGroupsResponse{ActionGroups: []*pb.ActionGroup{{
	Key:          "test-mcp",
	Title:        "Test MCP",
	ExecutorKind: pb.ExecutorKind_EXECUTOR_KIND_MCP,
	Available:    true,
	Actions: []*pb.Action{
		{Group: "test-mcp", Name: "alpha", Description: "test-alpha"},
		{Group: "test-mcp", Name: "internal", Description: "test-internal"},
	},
	DirectTools: []string{"alpha"},
}}}

func TestADirectCallAnswersWhenItsActionFinishesOrItsWaitPasses(t *testing.T) {
	running := testRequest(pb.ActionState_ACTION_STATE_RUNNING)
	for _, tc := range []struct {
		name   string
		frames []*pb.ActionRequest
		// finished ends the watch after its frames; otherwise the watch stays open past the direct wait.
		finished bool
		want     string
	}{
		{"finished within the wait", []*pb.ActionRequest{running, answeredRequest(t)}, true, "test answer"},
		{"still running after the wait", []*pb.ActionRequest{running}, false, "Action request " + testRequestID +
			` was approved and has not finished (running). This is not its result: call get_action_result(request_id="` +
			testRequestID + `", wait={"wait_seconds": 30}) to keep waiting.`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var mu sync.Mutex
			var watched []*pb.WatchActionRequestRequest
			var remaining []time.Duration
			recorder := &recordingCore{}
			session := newHarness(t, recorder.core(map[string]proto.Message{
				"ListActionGroups": directCatalog,
				"SubmitActionRequest": &pb.SubmitActionRequestResponse{
					ActionRequest: testRequest(pb.ActionState_ACTION_STATE_ALLOWED),
				},
			}, func(request *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
				deadline, _ := stream.Context().Deadline()
				mu.Lock()
				watched, remaining = append(watched, request), append(remaining, time.Until(deadline))
				mu.Unlock()
				for _, frame := range tc.frames {
					if err := stream.Send(&pb.WatchActionRequestResponse{ActionRequest: frame}); err != nil {
						return err
					}
				}
				if tc.finished {
					return nil
				}
				<-stream.Context().Done()
				return stream.Context().Err()
			}), Options{MaxWaitSeconds: 30, DirectWaitSeconds: 0.2}).connect(t, callerToken)
			result := callTool(t, session, "test-mcp__alpha", `{}`)
			if got := text(t, result); result.IsError || got != tc.want {
				t.Errorf("answer = %q (error %v), want %q", got, result.IsError, tc.want)
			}
			mu.Lock()
			defer mu.Unlock()
			want := &pb.WatchActionRequestRequest{RequestId: testRequestID, Until: pb.WatchUntil_WATCH_UNTIL_TERMINAL}
			if len(watched) != 1 || !proto.Equal(watched[0], want) || remaining[0] > 200*time.Millisecond {
				t.Errorf("watched %v with %v left, want %v within the direct wait", watched, remaining, want)
			}
			rpcs, callers := recorder.recorded()
			operations, operationCallers := withoutCatalogRPC(rpcs, callers)
			if wantRPCs := []string{"SubmitActionRequest", "WatchActionRequest"}; !slices.Equal(operations, wantRPCs) ||
				slices.ContainsFunc(callers, func(caller string) bool { return caller != "test-caller" }) ||
				slices.ContainsFunc(operationCallers, func(caller string) bool { return caller != "test-caller" }) ||
				len(rpcs)-len(operations) < 2 {
				t.Errorf("core saw %v as %v, want %v as test-caller plus the catalog snapshot and current-action check", rpcs, callers, wantRPCs)
			}
		})
	}
}

func TestANameNoGroupOffersAsADirectToolIsAnUnknownTool(t *testing.T) {
	recorder := &recordingCore{}
	session := newHarness(t, recorder.core(map[string]proto.Message{"ListActionGroups": directCatalog}, noWatch),
		Options{MaxWaitSeconds: 30, DirectWaitSeconds: 30}).connect(t, callerToken)
	for _, name := range []string{"test-mcp__internal", "test-mcp__test-unknown", "test-other__alpha"} {
		t.Run(name, func(t *testing.T) {
			if _, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: name, Arguments: map[string]any{}}); err == nil {
				t.Error("call answered, want an unknown tool error")
			}
		})
	}
	t.Run("arguments that are no object", func(t *testing.T) {
		if _, err := session.CallTool(t.Context(), &mcp.CallToolParams{Name: "test-mcp__alpha", Arguments: json.RawMessage(`[1]`)}); err == nil {
			t.Error("call answered, want an invalid params error")
		}
	})
	if rpcs, _ := recorder.recorded(); slices.Contains(rpcs, "SubmitActionRequest") {
		t.Errorf("core saw %v, want no submission", rpcs)
	}
}

func TestADirectListingTheCoreFailsStillListsTheGenericTools(t *testing.T) {
	session := newHarness(t, (&recordingCore{}).core(nil, noWatch), Options{MaxWaitSeconds: 30}).connect(t, callerToken)
	listed, err := session.ListTools(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var generic []toolDefinition
	if err := json.Unmarshal(toolsJSON, &generic); err != nil {
		t.Fatal(err)
	}
	if len(listed.Tools) != len(generic) || slices.ContainsFunc(listed.Tools, func(tool *mcp.Tool) bool {
		return strings.Contains(tool.Name, directSeparator)
	}) {
		t.Errorf("listed %d tools, want the %d generic ones", len(listed.Tools), len(generic))
	}
}

func TestInvalidDirectToolMetadataFailsWithASanitizedRetryableResponse(t *testing.T) {
	catalog := proto.Clone(directCatalog).(*pb.ListActionGroupsResponse)
	const schemaSentinel = "private-schema-sentinel"
	schema, err := structpb.NewStruct(map[string]any{"type": "string", "description": schemaSentinel})
	if err != nil {
		t.Fatal(err)
	}
	catalog.ActionGroups[0].Actions[0].InputSchema = schema
	var logs bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&logs, nil))
	h := newHarness(t, (&recordingCore{}).core(map[string]proto.Message{"ListActionGroups": catalog}, noWatch), Options{
		MaxWaitSeconds: 30,
		Logger:         logger,
	})
	response := post(t, h.url(), mcpHeaders("Bearer "+callerToken), toolsList)
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusServiceUnavailable || response.Header.Get("Retry-After") == "" ||
		strings.Contains(string(body), "test-mcp__alpha") || strings.Contains(string(body), "AddTool") || strings.Contains(string(body), schemaSentinel) {
		t.Errorf("answered %d with Retry-After %q and body %q, want sanitized retryable 503",
			response.StatusCode, response.Header.Get("Retry-After"), body)
	}
	if !strings.Contains(logs.String(), "test-mcp__alpha") || !strings.Contains(logs.String(), "SDK rejected direct tool metadata") ||
		strings.Contains(logs.String(), schemaSentinel) {
		t.Errorf("server log %q, want the tool identity and generic metadata error without schema text", logs.String())
	}
}

func TestDirectToolCatalogIsCompleteThroughTheSDKClient(t *testing.T) {
	group := &pb.ActionGroup{Key: "test-mcp", Title: "Test MCP", Available: true}
	for i := range mcp.DefaultPageSize + 1 {
		name := fmt.Sprintf("item-%04d", i)
		group.DirectTools = append(group.DirectTools, name)
		group.Actions = append(group.Actions, &pb.Action{Group: group.Key, Name: name})
	}
	catalog := &pb.ListActionGroupsResponse{ActionGroups: []*pb.ActionGroup{group}}
	policy := &pb.GetActionPolicyResponse{ActionPolicy: &pb.CallerActionPolicy{}}
	for _, name := range group.DirectTools {
		policy.ActionPolicy.AutoApprovable = append(policy.ActionPolicy.AutoApprovable, &pb.ActionIdentity{Group: group.Key, Name: name})
	}
	recorder := &recordingCore{}
	session := newHarness(t, recorder.core(map[string]proto.Message{
		"ListActionGroups": catalog,
		"GetActionPolicy":  policy,
	}, noWatch), Options{MaxWaitSeconds: 30}).connect(t, callerToken)
	// The handler rebuilds its caller-specific registry for each HTTP request. Use the SDK's iterator to check
	// that this integration preserves the complete catalog when it spans requests.
	var listedDirect []string
	for tool, err := range session.Tools(t.Context(), nil) {
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(tool.Name, directSeparator) {
			listedDirect = append(listedDirect, tool.Name)
		}
	}
	if len(listedDirect) != mcp.DefaultPageSize+1 {
		t.Fatalf("listed %d direct tools, want %d", len(listedDirect), mcp.DefaultPageSize+1)
	}
}

func TestAHiddenDirectToolStillReachesTheCoreDecidedOnlyCheck(t *testing.T) {
	var submission *pb.SubmitActionRequestRequest
	recorder := &recordingCore{}
	core := &fakeCore{
		unary: func(ctx context.Context, rpc string, request proto.Message) (proto.Message, error) {
			recorder.record(ctx, rpc)
			switch rpc {
			case "ListActionGroups":
				return directCatalog, nil
			case "GetActionPolicy":
				return &pb.GetActionPolicyResponse{ActionPolicy: &pb.CallerActionPolicy{Synced: true}}, nil
			case "SubmitActionRequest":
				submission = proto.Clone(request).(*pb.SubmitActionRequestRequest)
				return nil, status.Error(codes.PermissionDenied, "no policy approved this Action")
			default:
				return nil, status.Error(codes.Unimplemented, rpc)
			}
		},
		watch: noWatch,
	}
	session := newHarness(t, core, Options{MaxWaitSeconds: 30, DirectWaitSeconds: 30}).connect(t, callerToken)
	listed, err := session.ListTools(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if slices.ContainsFunc(listed.Tools, func(tool *mcp.Tool) bool { return tool.Name == "test-mcp__alpha" }) {
		t.Fatal("unapproved direct tool was listed")
	}
	result := callTool(t, session, "test-mcp__alpha", `{}`)
	if !result.IsError || !strings.Contains(text(t, result), "nothing was submitted") {
		t.Errorf("answer = %q (error %v), want the core's decided-only refusal", text(t, result), result.IsError)
	}
	if submission == nil || !submission.DecidedOnly {
		t.Errorf("core submission = %v, want DecidedOnly true", submission)
	}
}
