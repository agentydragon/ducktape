package mcpserver

import (
	"encoding/json"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"

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
			if wantRPCs := []string{"ListActionGroups", "SubmitActionRequest", "WatchActionRequest"}; !slices.Equal(rpcs, wantRPCs) ||
				slices.ContainsFunc(callers, func(caller string) bool { return caller != "test-caller" }) {
				t.Errorf("core saw %v as %v, want %v as test-caller", rpcs, callers, wantRPCs)
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
