package main

import (
	"context"
	"net"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func submittedReceipt(request *action_servicepb.SubmitActionRequestRequest) *action_servicepb.ActionRequest {
	created := testActionRequest("test-submitted-request", action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING)
	created.IdempotencyKey = request.GetIdempotencyKey()
	created.Action = request.GetAction()
	created.Arguments = request.GetArguments()
	created.Title = request.GetTitle()
	created.Description = request.Description
	return created
}

func TestSubmitWritesItsGeneratedKeyBeforeTheRPCAndGetFindsIt(t *testing.T) {
	fake, endpoint := newActionFake(t)
	c := newCLI(actionsEnv(endpoint), "")
	fake.submit = func(_ context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		if announced := decodeObject(t, c.stderr.String()); announced["idempotency_key"] != request.GetIdempotencyKey() {
			t.Error("the generated key was not written before SubmitActionRequest")
		}
		return &action_servicepb.SubmitActionRequestResponse{ActionRequest: submittedReceipt(request)}, nil
	}
	if code := c.run("actions", "submit", "test-group/test-action", "--title", "test title", "--json"); code != 0 {
		t.Fatalf("submit exit %d, stderr %q", code, c.stderr.String())
	}
	line := decodeObject(t, c.stderr.String())["idempotency_key"]
	key, ok := line.(string)
	if !ok || key == "" || decodeObject(t, c.stdout.String())["idempotencyKey"] != key {
		t.Fatalf("key announcement %v does not match stdout %q", line, c.stdout.String())
	}
	fake.listRequests = func(_ context.Context, request *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		if request.IdempotencyKey == nil || request.GetIdempotencyKey() != key {
			t.Errorf("lookup key = %v, want %q", request.IdempotencyKey, key)
		}
		created := testActionRequest("test-submitted-request", action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)
		created.IdempotencyKey = key
		return &action_servicepb.ListActionRequestsResponse{ActionRequests: []*action_servicepb.ActionRequest{created}}, nil
	}
	if code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "get", "--key", key, "--json"); code != 0 || decodeObject(t, stdout)["id"] != "test-submitted-request" {
		t.Errorf("get by generated key: exit %d, stdout %q", code, stdout)
	}
}

func TestSubmitAnnouncesAGeneratedKeyAsJSONUnderJSON(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(_ context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return &action_servicepb.SubmitActionRequestResponse{ActionRequest: submittedReceipt(request)}, nil
	}
	code, _, stderr := invoke(actionsEnv(endpoint), "--json", "actions", "submit", "test-group/test-action", "--title", "test title")
	if code != 0 || !strings.Contains(stderr, `"idempotency_key"`) {
		t.Errorf("exit %d, stderr %q; want the generated key as a JSON object", code, stderr)
	}
}

func TestSubmitWithAKeyAnnouncesNothing(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(_ context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return &action_servicepb.SubmitActionRequestResponse{ActionRequest: submittedReceipt(request)}, nil
	}
	code, _, stderr := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key")
	if code != 0 || stderr != "" {
		t.Errorf("exit %d, stderr %q; want no key announcement", code, stderr)
	}
}

func TestSubmitSendsTypedFieldsAndStructuredArguments(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(_ context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		if request.GetIdempotencyKey() != "test-key" || request.GetAction().GetGroup() != "test-group" || request.GetAction().GetName() != "test-action" ||
			request.GetTitle() != "test title" || request.Description == nil || *request.Description != "test detail" ||
			request.GetArguments().AsMap()["target"] != "test-target" {
			t.Errorf("SubmitActionRequest = %v", request)
		}
		return &action_servicepb.SubmitActionRequestResponse{ActionRequest: submittedReceipt(request)}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--description", "test detail", "--arguments", `{"target":"test-target"}`, "--key", "test-key", "--json")
	if code != 0 || decodeObject(t, stdout)["idempotencyKey"] != "test-key" {
		t.Errorf("exit %d, stdout %q", code, stdout)
	}
}

func TestSubmitOmitsAnAbsentDescription(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(_ context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		if request.Description != nil {
			t.Errorf("description = %q, want absent", request.GetDescription())
		}
		return &action_servicepb.SubmitActionRequestResponse{ActionRequest: submittedReceipt(request)}, nil
	}
	if code, _, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key"); code != 0 {
		t.Errorf("exit %d, want 0", code)
	}
}

func TestRepeatedKeyExitsConflictWithoutALookup(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(context.Context, *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return nil, status.Error(codes.AlreadyExists, "key is already used")
	}
	code, _, stderr := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key")
	if code != exitConflict || fake.count("SubmitActionRequest") != 1 || fake.count("ListActionRequests") != 0 || !strings.Contains(stderr, "actions get --key test-key") {
		t.Errorf("exit %d, calls %+v, stderr %q; want conflict with no lookup", code, fake.callLog(), stderr)
	}
}

func TestSubmitRefusalIsFinalAndDoesNotLookUpTheKey(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(context.Context, *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return nil, status.Error(codes.PermissionDenied, "policy refused")
	}
	code, _, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key")
	if code != exitRefused || fake.count("ListActionRequests") != 0 {
		t.Errorf("exit %d, calls %+v; want a refusal without lookup", code, fake.callLog())
	}
}

func TestLostSubmitResponseLooksUpTheSameKeyOnce(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(context.Context, *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return nil, status.Error(codes.Unavailable, "connection ended after admission")
	}
	fake.listRequests = func(_ context.Context, request *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		created := testActionRequest("test-submitted-request", action_servicepb.ActionState_ACTION_STATE_RUNNING)
		created.IdempotencyKey = request.GetIdempotencyKey()
		return &action_servicepb.ListActionRequestsResponse{ActionRequests: []*action_servicepb.ActionRequest{created}}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key", "--json")
	if got := decodeObject(t, stdout); code != 0 || got["id"] != "test-submitted-request" || fake.count("SubmitActionRequest") != 1 || fake.count("ListActionRequests") != 1 {
		t.Errorf("exit %d, stdout %q, calls %+v; want one submission and one lookup", code, stdout, fake.callLog())
	}
}

func TestSubmitThatCannotConfirmItsOutcomeDoesNotClaimTheKeyWasUnused(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(context.Context, *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return nil, status.Error(codes.Unavailable, "submission outcome unknown")
	}
	fake.listRequests = func(context.Context, *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		return nil, status.Error(codes.Unavailable, "lookup unavailable")
	}
	code, _, stderr := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key")
	if code != exitTransient || !strings.Contains(stderr, "outcome is unknown") || strings.Contains(stderr, "found no request") {
		t.Errorf("exit %d, stderr %q; want a transient unverified outcome", code, stderr)
	}
}

func TestSubmitWithAnAmbiguousResponseAndSuccessfulEmptyLookupExitsLost(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(context.Context, *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return nil, status.Error(codes.Unavailable, "submission response lost")
	}
	fake.listRequests = func(context.Context, *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		return &action_servicepb.ListActionRequestsResponse{}, nil
	}
	code, _, stderr := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key")
	if code != exitSubmitLost || !strings.Contains(stderr, "lookup by the key found no request") || fake.count("ListActionRequests") != 1 {
		t.Errorf("exit %d, stderr %q, calls %+v; want exit 15 after one successful empty lookup", code, stderr, fake.callLog())
	}
}

func TestSubmitWithAnUnreadableAnswerLooksUpTheKeyOnce(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(context.Context, *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return &action_servicepb.SubmitActionRequestResponse{}, nil
	}
	fake.listRequests = func(_ context.Context, request *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		created := testActionRequest("test-submitted-request", action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)
		created.IdempotencyKey = request.GetIdempotencyKey()
		return &action_servicepb.ListActionRequestsResponse{ActionRequests: []*action_servicepb.ActionRequest{created}}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key", "--json")
	if got := decodeObject(t, stdout); code != 0 || got["id"] != "test-submitted-request" || fake.count("ListActionRequests") != 1 {
		t.Errorf("exit %d, stdout %q, calls %+v", code, stdout, fake.callLog())
	}
}

func TestSubmitThatCannotReachTheServiceIsTransient(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	endpoint := "http://" + listener.Addr().String()
	_ = listener.Close()
	code, stdout, stderr := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key", "--json")
	if code != exitTransient || stdout != "" || errorOf(t, stderr).Source != sourceClient {
		t.Errorf("exit %d, stdout %q, stderr %q; want an uncommitted transient connect failure", code, stdout, stderr)
	}
}

func TestSubmitDeadlineAfterTheRPCStartsChecksTheSameKey(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(ctx context.Context, _ *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		<-ctx.Done()
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	code, _, stderr := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key", "--timeout", "0.2")
	if code != exitSubmitLost || fake.count("SubmitActionRequest") != 1 || fake.count("ListActionRequests") != 1 ||
		!strings.Contains(stderr, "found no request") {
		t.Errorf("exit %d, calls %+v, stderr %q; want one submit and one key lookup after the deadline", code, fake.callLog(), stderr)
	}
}

func TestSubmitWaitFollowsTheRequestWithWatchActionRequest(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(_ context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return &action_servicepb.SubmitActionRequestResponse{ActionRequest: submittedReceipt(request)}, nil
	}
	fake.watchRequest = func(request *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		if request.GetRequestId() != "test-submitted-request" || request.GetUntil() != action_servicepb.WatchUntil_WATCH_UNTIL_TERMINAL {
			t.Errorf("watch request = %v", request)
		}
		if err := stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: submittedReceipt(&action_servicepb.SubmitActionRequestRequest{
			IdempotencyKey: "test-key", Action: &action_servicepb.ActionIdentity{Group: "test-group", Name: "test-action"}, Title: "test title",
		})}); err != nil {
			return err
		}
		finished := testActionRequest("test-submitted-request", action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)
		finished.IdempotencyKey = "test-key"
		return stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: finished})
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key", "--wait", "--json")
	if got := decodeObject(t, stdout); code != 0 || got["state"] != "ACTION_STATE_SUCCEEDED" || fake.count("WatchActionRequest") != 1 {
		t.Errorf("exit %d, stdout %q, calls %+v; want the final receipt from one watch", code, stdout, fake.callLog())
	}
}

func TestSubmitWaitUntilDecisionStopsAtTheDecision(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(_ context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return &action_servicepb.SubmitActionRequestResponse{ActionRequest: submittedReceipt(request)}, nil
	}
	fake.watchRequest = func(request *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		if request.GetUntil() != action_servicepb.WatchUntil_WATCH_UNTIL_DECISION {
			t.Errorf("watch until = %s", request.GetUntil())
		}
		decision := testActionRequest("test-submitted-request", action_servicepb.ActionState_ACTION_STATE_ALLOWED)
		return stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: decision})
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key", "--wait", "--until", "decision", "--json")
	if got := decodeObject(t, stdout); code != 0 || got["state"] != "ACTION_STATE_ALLOWED" {
		t.Errorf("exit %d, stdout %q; want the allowed receipt", code, stdout)
	}
}

func TestSubmitWaitDoesNotWatchAReceiptThatAlreadyHolds(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(_ context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		allowed := submittedReceipt(request)
		allowed.State = action_servicepb.ActionState_ACTION_STATE_ALLOWED
		return &action_servicepb.SubmitActionRequestResponse{ActionRequest: allowed}, nil
	}
	code, _, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key", "--wait", "--until", "decision")
	if code != 0 || fake.count("WatchActionRequest") != 0 {
		t.Errorf("exit %d, calls %+v; want the submission receipt without opening a watch", code, fake.callLog())
	}
}

func TestSubmitWithoutWaitExitsZeroOnceRecorded(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.submit = func(_ context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
		return &action_servicepb.SubmitActionRequestResponse{ActionRequest: submittedReceipt(request)}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key", "--json")
	if got := decodeObject(t, stdout); code != 0 || got["state"] != "ACTION_STATE_DECISION_PENDING" || fake.count("WatchActionRequest") != 0 {
		t.Errorf("exit %d, stdout %q, calls %+v; want the pending submission receipt", code, stdout, fake.callLog())
	}
}

func TestArgumentsMustBeAnObjectBeforeSubmission(t *testing.T) {
	fake, endpoint := newActionFake(t)
	for _, value := range []string{`[]`, `null`, `4`, `"text"`} {
		code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "submit", "test-group/test-action", "--title", "test title", "--key", "test-key", "--arguments", value)
		if code != exitUsage || stdout != "" || fake.count("SubmitActionRequest") != 0 {
			t.Errorf("arguments %s: exit %d, stdout %q, calls %+v", value, code, stdout, fake.callLog())
		}
	}
}

func TestMalformedWatchReceiptDoesNotPrintPartialOutput(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: testActionRequest("", action_servicepb.ActionState_ACTION_STATE_RUNNING)}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "0")
	if code != exitDeadline || stdout == "" || fake.count("WatchActionRequest") != 0 {
		t.Errorf("exit %d, stdout %q, calls %+v", code, stdout, fake.callLog())
	}
}
