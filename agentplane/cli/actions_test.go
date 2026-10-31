package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/structpb"
)

func resultValue(t *testing.T, value string) *structpb.Value {
	t.Helper()
	parsed := &structpb.Value{}
	if err := protojson.Unmarshal([]byte(value), parsed); err != nil {
		t.Fatal(err)
	}
	return parsed
}

func requestWithResult(t *testing.T, value string) *action_servicepb.ActionRequest {
	t.Helper()
	request := testActionRequest(waitedID, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)
	request.Execution = &action_servicepb.Execution{Result: resultValue(t, value)}
	return request
}

func TestActionTextUsesGeneratedProtoJSON(t *testing.T) {
	group, err := groupLine(json.RawMessage(`{"key":"test-group","title":"Test","executorKind":"EXECUTOR_KIND_MCP","available":true,"actions":[{},{}]}`))
	if err != nil || group != "test-group  mcp  2 actions  Test" {
		t.Fatalf("group text = %q, error %v", group, err)
	}

	action, err := actionDetail(json.RawMessage(`{"group":"test-group","name":"echo","description":"Echo text","inputSchema":{"type":"object","properties":{"message":{"type":"string"}}}}`))
	if err != nil || !strings.Contains(action, "input schema:\n{") || !strings.Contains(action, `"message"`) {
		t.Fatalf("action detail = %q, error %v", action, err)
	}

	policy, err := policyText(json.RawMessage(`{"subject":{"namespace":"test","name":"agent"},"synced":true,"bindings":[{"name":"binding","expiresAt":"2026-01-02T03:04:05Z","policySets":[{"name":"set","generation":"1"}]}],"autoApproveIf":[{"binding":"binding","policySet":"set","source":"POLICY_SOURCE_RULES","index":0,"policy":{"exactActions":{"actions":{"test-group":{"values":["echo"]}}}}}]}`))
	wantPolicy := "subject test/agent  synced true\nbinding binding  sets set  expires 2026-01-02T03:04:05Z\nauto-approve set#0  exact_actions  test-group/echo"
	if err != nil || policy != wantPolicy {
		t.Fatalf("policy text = %q, error %v; want %q", policy, err, wantPolicy)
	}

	cancellation, err := cancellationLine(json.RawMessage(`{"outcome":"CANCELLATION_OUTCOME_CANCELLED","actionRequest":{"id":"request-id","action":{"group":"test-group","name":"echo"},"state":"ACTION_STATE_CANCELLED","title":"Stop it"}}`))
	if err != nil || cancellation != `cancelled  request-id  cancelled  test-group/echo  "Stop it"` {
		t.Fatalf("cancellation text = %q, error %v", cancellation, err)
	}
}

func TestGRPCTargetUsesTheURLDefaultPort(t *testing.T) {
	cases := []struct {
		origin string
		want   string
		bad    bool
	}{
		{"http://actions.example", "actions.example:80", false},
		{"https://actions.example", "actions.example:443", false},
		{"http://actions.example:8123", "actions.example:8123", false},
		{"https://[::1]", "[::1]:443", false},
		{"http://actions.example:", "", true},
		{"http://actions.example:0", "", true},
		{"http://actions.example:65536", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.origin, func(t *testing.T) {
			parsed, err := url.Parse(tc.origin)
			if err != nil {
				t.Fatal(err)
			}
			got, err := grpcTarget(parsed, "AGENTPLANE_ACTIONS_URL")
			if tc.bad {
				if err == nil {
					t.Fatalf("target = %q; want invalid origin", got)
				}
				return
			}
			if err != nil || got != tc.want {
				t.Fatalf("target = %q, error %v; want %q", got, err, tc.want)
			}
		})
	}
}

func TestSucceededActionExitsZeroWhateverItsResultSays(t *testing.T) {
	results := map[string]string{
		"a script that exited 3": `{"exit":{"exit_code":3},"stdout":"","stderr":"boom"}`,
		"an MCP tool error":      `{"content":[{"type":"text","text":"it broke"}],"isError":true}`,
		"no result":              `null`,
	}
	for name, value := range results {
		t.Run(name, func(t *testing.T) {
			fake, endpoint := newActionFake(t)
			fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
				return &action_servicepb.GetActionRequestResponse{ActionRequest: requestWithResult(t, value)}, nil
			}
			for _, args := range [][]string{
				{"actions", "result", waitedID},
				{"actions", "wait", waitedID},
			} {
				code, stdout, _ := invoke(actionsEnv(endpoint), append(args, "--json")...)
				if args[1] == "result" {
					var got, want any
					if err := json.Unmarshal([]byte(stdout), &got); err != nil {
						t.Fatalf("result JSON %q: %v", stdout, err)
					}
					if err := json.Unmarshal([]byte(value), &want); err != nil {
						t.Fatalf("test result JSON %q: %v", value, err)
					}
					if code != 0 || !reflect.DeepEqual(got, want) {
						t.Errorf("%v: exit %d, stdout %q; want exit 0 and %s", args, code, stdout, value)
					}
					continue
				}
				got := decodeObject(t, stdout)
				execution, ok := got["execution"].(map[string]any)
				_, hasResult := execution["result"]
				if code != 0 || got["id"] != waitedID || got["state"] != "ACTION_STATE_SUCCEEDED" || !ok || !hasResult {
					t.Errorf("%v: exit %d, stdout %q; want its successful receipt", args, code, stdout)
				}
			}
		})
	}
}

func TestResultPrintsTextBlocksAndOtherwiseIndentedJSON(t *testing.T) {
	cases := []struct{ name, result, want string }{
		{"text blocks", `{"content":[{"type":"text","text":"one"},{"type":"image","data":"x"},{"type":"text","text":"two"}],"isError":false}`, "one\ntwo"},
		{"no text blocks", `{"exit":{"exit_code":0}}`, "{\n  \"exit\": {\n    \"exit_code\": 0\n  }\n}"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fake, endpoint := newActionFake(t)
			fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
				return &action_servicepb.GetActionRequestResponse{ActionRequest: requestWithResult(t, tc.result)}, nil
			}
			code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "result", waitedID)
			if code != 0 || strings.TrimSpace(stdout) != tc.want {
				t.Errorf("exit %d, stdout %q; want %q", code, stdout, tc.want)
			}
		})
	}
}

func TestResultOfAnUnsuccessfulActionPrintsTheReceipt(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_DENIED)}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "result", waitedID, "--json")
	if got := decodeObject(t, stdout); code != exitDenied || got["id"] != waitedID {
		t.Errorf("exit %d, stdout %q; want exit 11 and the receipt", code, stdout)
	}
}

func TestCancelExitsByOutcome(t *testing.T) {
	cases := []struct {
		outcome  action_servicepb.CancellationOutcome
		wantExit int
	}{
		{action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_CANCELLED, 0},
		{action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_ALREADY_CANCELLED, 0},
		{action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_TOO_LATE, exitTooLate},
		{action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_ALREADY_FINISHED, exitAlreadyFinished},
	}
	for _, tc := range cases {
		fake, endpoint := newActionFake(t)
		fake.cancelRequest = func(_ context.Context, request *action_servicepb.CancelActionRequestRequest) (*action_servicepb.CancelActionRequestResponse, error) {
			return &action_servicepb.CancelActionRequestResponse{
				Outcome:       tc.outcome,
				ActionRequest: testActionRequest(request.GetRequestId(), action_servicepb.ActionState_ACTION_STATE_CANCELLED),
			}, nil
		}
		code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "cancel", waitedID, "--json")
		got := decodeObject(t, stdout)
		if code != tc.wantExit || got["outcome"] != tc.outcome.String() || got["actionRequest"] == nil || fake.count("CancelActionRequest") != 1 {
			t.Errorf("%s: exit %d, stdout %q; want exit %d and the generated response", tc.outcome, code, stdout, tc.wantExit)
		}
	}
}

func TestCancelOfAnUnknownRequestExitsNotFound(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.cancelRequest = func(context.Context, *action_servicepb.CancelActionRequestRequest) (*action_servicepb.CancelActionRequestResponse, error) {
		return nil, status.Error(codes.NotFound, "Action request not found")
	}
	if code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "cancel", missingID); code != exitNotFound || stdout != "" {
		t.Errorf("exit %d, stdout %q; want exit 5", code, stdout)
	}
}

func TestGetExitsZeroForAnyState(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_DENIED)}, nil
	}
	if code, _, _ := invoke(actionsEnv(endpoint), "actions", "get", waitedID); code != 0 {
		t.Errorf("get of a denied request: exit %d, want 0", code)
	}
}

func TestGetByKeyThatMatchesNothingExitsNotFound(t *testing.T) {
	fake, endpoint := newActionFake(t)
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "get", "--key", "nobody")
	if code != exitNotFound || stdout != "" || fake.count("ListActionRequests") != 1 {
		t.Errorf("exit %d, stdout %q, ListActionRequests calls %d; want exit 5 after one lookup", code, stdout, fake.count("ListActionRequests"))
	}
}

func TestJSONOutputIsGeneratedProtoJSON(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.listRequests = func(context.Context, *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		return &action_servicepb.ListActionRequestsResponse{ActionRequests: []*action_servicepb.ActionRequest{
			receiptAt("test-request-one", action_servicepb.ActionState_ACTION_STATE_SUCCEEDED),
			receiptAt("test-request-two", action_servicepb.ActionState_ACTION_STATE_FAILED),
		}}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "requests", "--json")
	got := jsonLines(t, stdout)
	if code != 0 || len(got) != 2 || got[0]["id"] != "test-request-one" || got[0]["state"] != "ACTION_STATE_SUCCEEDED" {
		t.Errorf("exit %d, output %v; want the generated ActionRequest objects", code, got)
	}
}

func TestUnaryJSONOutputIsOneValue(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 200, `{"subject":{"namespace":"test-ns","name":"test-sa"},"policies":[]}`)
	})
	code, stdout, _ := invoke(egressEnv(f.URL), "egress", "rules", "--json")
	if code != 0 || strings.TrimSpace(stdout) != `{"subject":{"namespace":"test-ns","name":"test-sa"},"policies":[]}` {
		t.Errorf("exit %d, stdout %q", code, stdout)
	}
}

func TestListFlattensTheCatalogIntoActions(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.listGroups = func(context.Context, *action_servicepb.ListActionGroupsRequest) (*action_servicepb.ListActionGroupsResponse, error) {
		return &action_servicepb.ListActionGroupsResponse{ActionGroups: []*action_servicepb.ActionGroup{
			{Key: "g1", Title: "One", ExecutorKind: action_servicepb.ExecutorKind_EXECUTOR_KIND_MCP, Available: true, Actions: []*action_servicepb.Action{
				{Group: "g1", Name: "a", Description: "first", InputSchema: &structpb.Struct{Fields: map[string]*structpb.Value{}}},
				{Group: "g1", Name: "b", Description: "second", InputSchema: &structpb.Struct{Fields: map[string]*structpb.Value{}}},
			}},
			{Key: "g2", Title: "Two", ExecutorKind: action_servicepb.ExecutorKind_EXECUTOR_KIND_POD, Available: true, Actions: []*action_servicepb.Action{
				{Group: "g2", Name: "c", Description: "third", InputSchema: &structpb.Struct{Fields: map[string]*structpb.Value{}}},
			}},
		}}, nil
	}
	for _, tc := range []struct {
		args     []string
		wantExit int
		want     []string
	}{
		{[]string{"list"}, 0, []string{"a", "b", "c"}},
		{[]string{"list", "g2"}, 0, []string{"c"}},
		{[]string{"list", "missing"}, exitNotFound, nil},
	} {
		code, stdout, _ := invoke(actionsEnv(endpoint), append(append([]string{"actions"}, tc.args...), "--json")...)
		var names []string
		for _, action := range jsonLines(t, stdout) {
			names = append(names, action["name"].(string))
		}
		if code != tc.wantExit || strings.Join(names, ",") != strings.Join(tc.want, ",") {
			t.Errorf("%v: exit %d, actions %v; want exit %d and %v", tc.args, code, names, tc.wantExit, tc.want)
		}
	}
	if code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "groups", "--json"); code != 0 || len(jsonLines(t, stdout)) != 2 {
		t.Errorf("groups: exit %d, stdout %q; want one line per group", code, stdout)
	}
}

func TestShowAndPolicyUseGeneratedCalls(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getAction = func(_ context.Context, request *action_servicepb.GetActionRequest) (*action_servicepb.GetActionResponse, error) {
		return &action_servicepb.GetActionResponse{Action: &action_servicepb.Action{
			Group: request.GetGroupKey(), Name: request.GetActionKey(), Description: "test description",
			InputSchema: &structpb.Struct{Fields: map[string]*structpb.Value{}},
		}}, nil
	}
	invoke(actionsEnv(endpoint), "actions", "show", "test-group/test-action")
	invoke(actionsEnv(endpoint), "actions", "policy")
	calls := fake.callLog()
	if len(calls) != 2 || calls[0].method != "GetAction" || calls[1].method != "GetActionPolicy" {
		t.Errorf("calls %+v; want GetAction and GetActionPolicy", calls)
	}
}

func TestRequestsFollowPagesAndMapStateFilters(t *testing.T) {
	for _, tc := range []struct {
		limit int
		want  int
	}{{0, 300}, {200, 200}, {3, 3}, {500, 300}} {
		fake, endpoint := newActionFake(t)
		requests := make([]*action_servicepb.ActionRequest, 300)
		for i := range requests {
			requests[i] = receiptAt(fmt.Sprintf("test-request-%03d", i), action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)
		}
		fake.listRequests = func(_ context.Context, request *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
			if len(request.GetState()) != 1 || request.GetState()[0] != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED {
				t.Errorf("state filter = %v", request.GetState())
			}
			start := 0
			if request.GetAfterId() != "" {
				start = -1
				for i, item := range requests {
					if item.GetId() == request.GetAfterId() {
						start = i + 1
					}
				}
				if start < 0 {
					return nil, status.Error(codes.NotFound, "unknown after id")
				}
			}
			end := len(requests)
			if request.Limit != nil {
				end = min(start+int(request.GetLimit()), end)
			}
			return &action_servicepb.ListActionRequestsResponse{ActionRequests: requests[start:end]}, nil
		}
		code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "requests", "--state", "succeeded", "--limit", strconv.Itoa(tc.limit), "--json")
		if got := jsonLines(t, stdout); code != 0 || len(got) != tc.want {
			t.Errorf("--limit %d: exit %d with %d items, want %d", tc.limit, code, len(got), tc.want)
		}
		for _, call := range fake.callLog() {
			if call.method != "ListActionRequests" {
				t.Fatalf("call %s, want ListActionRequests", call.method)
			}
		}
	}
}

func TestRequestsTextModeSaysWhereToResume(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.listRequests = func(context.Context, *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		return &action_servicepb.ListActionRequestsResponse{ActionRequests: []*action_servicepb.ActionRequest{
			receiptAt("test-request-one", action_servicepb.ActionState_ACTION_STATE_SUCCEEDED),
		}}, nil
	}
	code, stdout, stderr := invoke(actionsEnv(endpoint), "actions", "requests", "--limit", "1")
	if code != 0 || !strings.Contains(stdout, "test-request-one") || !strings.Contains(stderr, "--after test-request-one") {
		t.Errorf("exit %d, stdout %q, stderr %q; want the last ID as the resume cursor", code, stdout, stderr)
	}
}

func TestRequestsAfterAnUnknownIDExitsNotFound(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.listRequests = func(context.Context, *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		return nil, status.Error(codes.NotFound, "unknown after id")
	}
	if code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "requests", "--after", "unknown"); code != exitNotFound || stdout != "" {
		t.Errorf("exit %d, stdout %q; want exit 5", code, stdout)
	}
}

func TestEventsFollowPagesFromASequence(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.listEvents = func(_ context.Context, request *action_servicepb.ListActionRequestEventsRequest) (*action_servicepb.ListActionRequestEventsResponse, error) {
		if request.GetRequestId() != waitedID {
			return nil, status.Error(codes.InvalidArgument, "wrong request id")
		}
		if request.GetAfterSequence() == 4 {
			return &action_servicepb.ListActionRequestEventsResponse{Events: []*action_servicepb.ActionEvent{
				{Sequence: 5, State: action_servicepb.ActionState_ACTION_STATE_RUNNING},
			}}, nil
		}
		return &action_servicepb.ListActionRequestEventsResponse{}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "events", waitedID, "--after", "4", "--json")
	if got := jsonLines(t, stdout); code != 0 || len(got) != 1 || got[0]["sequence"] != float64(5) {
		t.Errorf("exit %d, stdout %q; want sequence 5", code, stdout)
	}
}

func TestActionsSendTheConfiguredBearerAsMetadata(t *testing.T) {
	fake, endpoint := newActionFake(t)
	_, _, _ = invoke(actionsEnv(endpoint), "actions", "groups")
	calls := fake.callLog()
	if len(calls) != 1 || strings.Join(calls[0].authorization, ",") != "Bearer "+testCredential {
		t.Errorf("calls %+v; want the credential as Authorization metadata", calls)
	}
}

func TestActionBaseURLRejectsAPathBeforeCallingTheService(t *testing.T) {
	fake, endpoint := newActionFake(t)
	env := actionsEnv(endpoint + "/prefix")
	code, stdout, stderr := invoke(env, "actions", "groups")
	if code != exitUsage || stdout != "" || fake.count("ListActionGroups") != 0 || !strings.Contains(stderr, "must not include a path") {
		t.Errorf("exit %d, stdout %q, stderr %q, calls %+v", code, stdout, stderr, fake.callLog())
	}
}

func TestLegacyHTTPConfigurationStillWorksForEgress(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, request seen) {
		if request.path != "/v1/rules" {
			t.Errorf("path = %q", request.path)
		}
		reply(w, 200, `{"subject":{"namespace":"test-ns","name":"test-sa"},"policies":[]}`)
	})
	code, _, _ := invoke(egressEnv(f.URL), "egress", "rules", "--json")
	if code != 0 || len(f.requests(http.MethodGet)) != 1 {
		t.Errorf("exit %d after %d requests", code, f.count())
	}
}

func TestActionLookupByKeyUsesTheGeneratedRequestFilter(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.listRequests = func(_ context.Context, request *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		if request.IdempotencyKey == nil || request.GetIdempotencyKey() != "test-key" {
			t.Errorf("idempotency key filter = %v", request.IdempotencyKey)
		}
		return &action_servicepb.ListActionRequestsResponse{ActionRequests: []*action_servicepb.ActionRequest{
			receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED),
		}}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "get", "--key", "test-key", "--json")
	if code != 0 || decodeObject(t, stdout)["id"] != waitedID {
		t.Errorf("exit %d, stdout %q", code, stdout)
	}
}

func TestEventCursorRejectsValuesOutsideTheProtoRange(t *testing.T) {
	fake, endpoint := newActionFake(t)
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "events", waitedID, "--after", "2147483648")
	if code != exitUsage || stdout != "" || fake.count("ListActionRequestEvents") != 0 {
		t.Errorf("exit %d, stdout %q, calls %+v", code, stdout, fake.callLog())
	}
}
