package main

import (
	"context"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
)

const waitedID = "00000000-0000-0000-0000-0000000000a1"

func receiptAt(id string, state action_servicepb.ActionState) *action_servicepb.ActionRequest {
	request := testActionRequest(id, state)
	request.IdempotencyKey = "test-key"
	return request
}

func TestWaitAndResultExitByOutcome(t *testing.T) {
	cases := []struct {
		state    action_servicepb.ActionState
		wantExit int
	}{
		{action_servicepb.ActionState_ACTION_STATE_SUCCEEDED, 0},
		{action_servicepb.ActionState_ACTION_STATE_DENIED, 11},
		{action_servicepb.ActionState_ACTION_STATE_FAILED, 12},
		{action_servicepb.ActionState_ACTION_STATE_CANCELLED, 13},
		{action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN, 14},
	}
	for _, tc := range cases {
		fake, endpoint := newActionFake(t)
		fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
			return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, tc.state)}, nil
		}
		for _, command := range []string{"wait", "result"} {
			code, stdout, _ := invoke(actionsEnv(endpoint), "actions", command, waitedID, "--json")
			var wantReceipt bool
			if command == "wait" || tc.state != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED {
				wantReceipt = true
			}
			if got := jsonLines(t, stdout); code != tc.wantExit || wantReceipt && (len(got) != 1 || got[0]["state"] != tc.state.String()) {
				t.Errorf("%s of state %s: exit %d, stdout %q; want exit %d and the receipt", command, tc.state, code, stdout, tc.wantExit)
			}
		}
	}
}

func TestWaitUntilDecisionStopsAtTheDecision(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING)}, nil
	}
	fake.watchRequest = func(request *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		if request.GetUntil() != action_servicepb.WatchUntil_WATCH_UNTIL_DECISION {
			t.Errorf("WatchActionRequest until = %s", request.GetUntil())
		}
		return stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_ALLOWED)})
	}
	if code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--until", "decision", "--json"); code != 0 || decodeObject(t, stdout)["state"] != "ACTION_STATE_ALLOWED" {
		t.Errorf("exit %d, stdout %q; want the allowed receipt and exit 0", code, stdout)
	}
}

func TestWaitExits10AtTheDeadlineWithTheCurrentReceipt(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}, nil
	}
	started := make(chan struct{})
	fake.watchRequest = func(_ *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		if err := stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}); err != nil {
			return err
		}
		close(started)
		<-stream.Context().Done()
		return status.FromContextError(stream.Context().Err()).Err()
	}
	done := make(chan struct {
		code           int
		stdout, stderr string
	}, 1)
	go func() {
		code, stdout, stderr := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "0.2", "--json")
		done <- struct {
			code           int
			stdout, stderr string
		}{code, stdout, stderr}
	}()
	<-started
	got := <-done
	if receipt := decodeObject(t, got.stdout); got.code != exitDeadline || receipt["state"] != "ACTION_STATE_RUNNING" {
		t.Errorf("exit %d, stdout %q; want exit 10 and the current receipt", got.code, got.stdout)
	}
}

func TestWaitWithTimeoutZeroReadsOnce(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "0", "--json")
	if got := decodeObject(t, stdout); code != exitDeadline || got["state"] != "ACTION_STATE_RUNNING" || fake.count("GetActionRequest") != 1 || fake.count("WatchActionRequest") != 0 {
		t.Errorf("exit %d, stdout %q, calls %+v; want one GetActionRequest and no watch", code, stdout, fake.callLog())
	}
}

func TestWaitByKeyResolvesTheIDBeforeOpeningAWatch(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.listRequests = func(_ context.Context, request *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
		if request.GetIdempotencyKey() != "test-key" {
			t.Errorf("idempotency key = %q", request.GetIdempotencyKey())
		}
		return &action_servicepb.ListActionRequestsResponse{ActionRequests: []*action_servicepb.ActionRequest{
			receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING),
		}}, nil
	}
	fake.watchRequest = func(request *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		if request.GetRequestId() != waitedID || request.GetUntil() != action_servicepb.WatchUntil_WATCH_UNTIL_TERMINAL {
			t.Errorf("watch request = %v", request)
		}
		return stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)})
	}
	if code, _, _ := invoke(actionsEnv(endpoint), "actions", "wait", "--key", "test-key", "--timeout", "5"); code != 0 {
		t.Errorf("exit %d, want 0", code)
	}
	calls := fake.callLog()
	if len(calls) != 2 || calls[0].method != "ListActionRequests" || calls[1].method != "WatchActionRequest" {
		t.Errorf("calls = %+v; want key lookup then one watch", calls)
	}
}

func TestWaitByUnknownKeyExitsNotFound(t *testing.T) {
	fake, endpoint := newActionFake(t)
	if code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "wait", "--key", "nobody"); code != exitNotFound || stdout != "" || fake.count("WatchActionRequest") != 0 {
		t.Errorf("exit %d, stdout %q, calls %+v; want not found before a watch", code, stdout, fake.callLog())
	}
}

func TestWaitRetriesAnUnavailableWatchWithinTheBudget(t *testing.T) {
	fake, endpoint := newActionFake(t)
	var attempts atomic.Int32
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}, nil
	}
	fake.watchRequest = func(_ *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		if attempts.Add(1) == 1 {
			if err := stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}); err != nil {
				return err
			}
			return status.Error(codes.Unavailable, "test stream interruption")
		}
		return stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)})
	}
	code, _, _ := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "5")
	if code != 0 || attempts.Load() != 2 {
		t.Errorf("exit %d after %d watches; want success after reconnecting once", code, attempts.Load())
	}
}

func TestWaitThatLosesTheServiceExits3WithTheLastReceipt(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}, nil
	}
	fake.watchRequest = func(_ *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		if err := stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}); err != nil {
			return err
		}
		st := status.New(codes.Unavailable, "test service outage")
		st, err := st.WithDetails(&errdetails.RetryInfo{RetryDelay: durationpb.New(5 * time.Second)})
		if err != nil {
			return err
		}
		return st.Err()
	}
	code, stdout, stderr := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "0.2", "--json")
	if got := decodeObject(t, stdout); code != exitTransient || got["state"] != "ACTION_STATE_RUNNING" || errorOf(t, stderr).Code != "UNAVAILABLE" {
		t.Errorf("exit %d, stdout %q, stderr %q; want exit 3 with the running receipt", code, stdout, stderr)
	}
}

func TestWaitThatNeverReadsAReceiptExitsTransientWithoutOutput(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(ctx context.Context, _ *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		<-ctx.Done()
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "0.2")
	if code != exitTransient || stdout != "" || fake.count("WatchActionRequest") != 0 {
		t.Errorf("exit %d, stdout %q, calls %+v; want no receipt and no watch", code, stdout, fake.callLog())
	}
}

func TestWaitDoesNotRetryARefusal(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return nil, status.Error(codes.PermissionDenied, "test refusal")
	}
	code, _, _ := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "5")
	if code != exitRefused || fake.count("GetActionRequest") != 1 || fake.count("WatchActionRequest") != 0 {
		t.Errorf("exit %d, calls %+v; want a final refusal after one read", code, fake.callLog())
	}
}

func TestWaitRefusesAStateItDoesNotKnow(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState(99))}, nil
	}
	code, stdout, _ := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "5")
	if code != exitRefused || stdout != "" || fake.count("WatchActionRequest") != 0 {
		t.Errorf("exit %d, stdout %q, calls %+v; want an immediate unusable response", code, stdout, fake.callLog())
	}
}

func TestWaitReconnectsAfterTheBearerExpiresMidstream(t *testing.T) {
	fake, endpoint := newActionFake(t)
	var attempts atomic.Int32
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}, nil
	}
	fake.watchRequest = func(_ *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		if attempts.Add(1) == 1 {
			if err := stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}); err != nil {
				return err
			}
			return status.Error(codes.Unauthenticated, "projected token expired")
		}
		return stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED)})
	}
	code, _, _ := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "5")
	if code != 0 || attempts.Load() != 2 {
		t.Errorf("exit %d after %d watches; want a fresh call and success", code, attempts.Load())
	}
}

func TestSigtermEndsAWatchedRequestAndCancelsNothing(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}, nil
	}
	arrived := make(chan struct{})
	var once sync.Once
	fake.watchRequest = func(_ *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		if err := stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}); err != nil {
			return err
		}
		once.Do(func() { close(arrived) })
		<-stream.Context().Done()
		return status.FromContextError(stream.Context().Err()).Err()
	}
	c := newCLI(actionsEnv(endpoint), "")
	exited := make(chan int, 1)
	go func() { exited <- c.run("actions", "wait", waitedID, "--timeout", "60") }()
	select {
	case <-arrived:
	case <-time.After(10 * time.Second):
		t.Fatal("the wait never reached WatchActionRequest")
	}
	if err := syscall.Kill(syscall.Getpid(), syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	select {
	case code := <-exited:
		if code != 128+int(syscall.SIGTERM) || c.stdout.String() != "" || fake.count("CancelActionRequest") != 0 {
			t.Errorf("exit %d, stdout %q; want 143, no output and no cancellation", code, c.stdout.String())
		}
	case <-time.After(10 * time.Second):
		t.Fatal("SIGTERM did not end the wait")
	}
}
