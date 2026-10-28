package orchestrationv1

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1/contract"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

func turnArguments(t *testing.T, sandbox contract.SandboxRef, session string, extra map[string]any) string {
	t.Helper()
	args := map[string]any{"sandbox": sandbox, "session_id": session}
	for key, value := range extra {
		args[key] = value
	}
	encoded, err := json.Marshal(args)
	if err != nil {
		t.Fatal(err)
	}
	return string(encoded)
}

func turnEntries(through uint64, events ...*eventpb.Event) *historypb.FollowSessionResponse {
	entries := make([]*eventlogpb.EventEntry, 0, len(events))
	for i, event := range events {
		entries = append(entries, &eventlogpb.EventEntry{Cursor: through - uint64(len(events)-1-i), Event: event})
	}
	return &historypb.FollowSessionResponse{ThroughCursor: through, Frame: &historypb.FollowSessionResponse_Entries{Entries: &historypb.Entries{Entries: entries}}}
}

func TestReadTurnsGroupsConfirmedInputToolsAndFinalAssistantText(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-turn-reader")
	session := uuid.NewString()
	f.history.hold(summaryOf(f, session, sandbox, nil))
	f.history.follow = func(request *historypb.FollowSessionRequest, stream grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
		if request.GetBack().GetKind() != historypb.EventKind_EVENT_KIND_TURN_STARTED || request.GetBack().GetCount() != 2 || request.GetFollow() {
			t.Errorf("read start = %v", request)
		}
		// Split an item's start and completion across pages; reasoning and tools must not replace the final text.
		if err := stream.Send(turnEntries(
			4,
			&eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "test-turn"}}},
			&eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{TurnId: "test-turn", Text: "test input"}}},
			&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: "test-text", Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT}}},
			&eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{ItemId: "test-text", Outcome: &eventpb.ItemCompleted_Text{Text: "earlier"}}}},
		)); err != nil {
			return err
		}
		if err := stream.Send(turnEntries(
			10,
			&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: "test-tool", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "test_tool"}}},
			&eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{ItemId: "test-text", Outcome: &eventpb.ItemCompleted_Text{Text: "final test response"}}}},
			&eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: "test-reason", Kind: eventpb.ItemKind_ITEM_KIND_REASONING}}},
			&eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{ItemId: "test-reason", Outcome: &eventpb.ItemCompleted_Text{Text: "private reasoning"}}}},
			&eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: "test-turn", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}}},
			&eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "test-running"}}},
		)); err != nil {
			return err
		}
		return stream.Send(&historypb.FollowSessionResponse{ThroughCursor: 12, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
	}
	got := resultOf[Turns](f.attempt(t, f.caller, contract.ActionReadTurns, turnArguments(t, sandbox, session, map[string]any{"count": 2})))
	if len(got.Turns) != 2 {
		t.Fatalf("turns = %+v", got)
	}
	turn := got.Turns[0]
	if len(turn.Input) != 1 || turn.Input[0].Text != "test input" || len(turn.ToolCalls) != 1 || turn.ToolCalls[0].Name != "test_tool" || turn.FinalAssistantMessage == nil || *turn.FinalAssistantMessage != "final test response" || turn.Status == nil || *turn.Status != "TURN_STATUS_COMPLETED" {
		t.Errorf("completed turn = %+v", turn)
	}
	if got.Turns[1].Status != nil {
		t.Errorf("running turn status = %v", got.Turns[1].Status)
	}
	if cursor, err := resumeCursor(got.ResumeToken, sandbox, session); err != nil || cursor != 12 {
		t.Errorf("resume = %d, %v", cursor, err)
	}
}

func TestWaitRenewsAndStopsAtFirstCompletionInABatch(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-turn-waiter")
	session := uuid.NewString()
	f.history.hold(summaryOf(f, session, sandbox, nil))
	var calls atomic.Int32
	f.history.follow = func(request *historypb.FollowSessionRequest, stream grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
		if !request.GetFollow() || len(request.GetKinds()) != 1 || request.GetKinds()[0] != historypb.EventKind_EVENT_KIND_TURN_COMPLETED {
			t.Errorf("wait request = %v", request)
		}
		if calls.Add(1) == 1 {
			if request.GetAfterCursor() != 2 {
				t.Errorf("initial cursor %d", request.GetAfterCursor())
			}
			return stream.Send(&historypb.FollowSessionResponse{ThroughCursor: 5, Frame: &historypb.FollowSessionResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}})
		}
		if request.GetAfterCursor() != 5 {
			t.Errorf("renewal cursor %d", request.GetAfterCursor())
		}
		return stream.Send(turnEntries(
			7,
			&eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: "test-first", Status: eventpb.TurnStatus_TURN_STATUS_FAILED, Error: "test failure"}}},
			&eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: "test-next", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}}},
		))
	}
	got := resultOf[TurnWait](f.attempt(t, f.caller, contract.ActionWaitForTurn, turnArguments(t, sandbox, session, map[string]any{"resume_token": resumeToken(sandbox, session, 2), "wait_seconds": 1})))
	if !got.Completed || got.Turn == nil || got.Turn.TurnID != "test-first" || got.Turn.Status != "TURN_STATUS_FAILED" || got.Turn.Error == nil {
		t.Fatalf("wait = %+v", got)
	}
	if cursor, err := resumeCursor(got.ResumeToken, sandbox, session); err != nil || cursor != 6 {
		t.Errorf("resume = %d, %v; want first completion6, leaving7 for next call", cursor, err)
	}
}

func TestWaitDeadlineReturnsNewestProgressAndRejectsBareEOF(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-bounded-wait")
	session := uuid.NewString()
	f.history.hold(summaryOf(f, session, sandbox, nil))
	f.executor.readBudget = 100 * time.Millisecond
	f.history.follow = func(_ *historypb.FollowSessionRequest, stream grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
		if _, ok := stream.Context().Deadline(); !ok {
			t.Error("follow has no deadline")
		}
		if err := stream.Send(&historypb.FollowSessionResponse{ThroughCursor: 9, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}}); err != nil {
			return err
		}
		<-stream.Context().Done()
		return status.FromContextError(stream.Context().Err()).Err()
	}
	args := turnArguments(t, sandbox, session, map[string]any{"resume_token": resumeToken(sandbox, session, 2), "wait_seconds": 30})
	got := resultOf[TurnWait](f.attempt(t, f.caller, contract.ActionWaitForTurn, args))
	if got.Completed || got.Turn != nil {
		t.Fatalf("wait = %+v", got)
	}
	if cursor, err := resumeCursor(got.ResumeToken, sandbox, session); err != nil || cursor != 9 {
		t.Errorf("resume = %d %v", cursor, err)
	}
	f.history.follow = func(_ *historypb.FollowSessionRequest, _ grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
		return nil
	}
	if kind := f.attempt(t, f.caller, contract.ActionWaitForTurn, args).failure(); kind != "read_failed" {
		t.Errorf("bare EOF failure = %s", kind)
	}
}

// History request timeouts before the wait deadline must remain read failures.
func TestWaitHistoryTimeoutBeforeDeadlineFails(t *testing.T) {
	for _, renew := range []bool{false, true} {
		t.Run(map[bool]string{false: "receiving", true: "after_reconnect"}[renew], func(t *testing.T) {
			f := newFixture(t)
			sandbox := f.launch(t, "test-history-deadline")
			session := uuid.NewString()
			f.history.hold(summaryOf(f, session, sandbox, nil))
			var calls atomic.Int32
			f.history.follow = func(request *historypb.FollowSessionRequest, stream grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
				if calls.Add(1) == 1 {
					frame := &historypb.FollowSessionResponse{ThroughCursor: 9, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}}
					if renew {
						frame.Frame = &historypb.FollowSessionResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}
					}
					if err := stream.Send(frame); err != nil {
						return err
					}
					if renew {
						return nil
					}
				} else if request.GetAfterCursor() != 9 {
					t.Errorf("renewal cursor = %d", request.GetAfterCursor())
				}
				return status.Error(codes.DeadlineExceeded, "test History deadline before client timeout")
			}
			if kind := f.attempt(t, f.caller, contract.ActionWaitForTurn, turnArguments(t, sandbox, session, map[string]any{"resume_token": resumeToken(sandbox, session, 2), "wait_seconds": 30})).failure(); kind != "read_failed" {
				t.Errorf("History timeout failure = %s", kind)
			}
		})
	}
}

func TestTurnReadsRefuseForeignSessionsAndTokens(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-read-scope")
	other := f.launch(t, "test-other-scope")
	session := uuid.NewString()
	f.history.hold(summaryOf(f, session, other, nil))
	for _, action := range []string{contract.ActionReadTurns, contract.ActionWaitForTurn} {
		extra := map[string]any{"count": 1}
		if action == contract.ActionWaitForTurn {
			extra = map[string]any{"resume_token": resumeToken(sandbox, session, 0), "wait_seconds": 1}
		}
		if kind := f.attempt(t, f.caller, action, turnArguments(t, sandbox, session, extra)).failure(); kind != "session_not_found" {
			t.Errorf("%s foreign read = %s", action, kind)
		}
	}
	f.history.hold(summaryOf(f, session, sandbox, nil))
	for _, token := range []string{"malformed", resumeToken(other, session, 0), resumeToken(sandbox, uuid.NewString(), 0)} {
		if kind := f.attempt(t, f.caller, contract.ActionWaitForTurn, turnArguments(t, sandbox, session, map[string]any{"resume_token": token, "wait_seconds": 1})).failure(); kind != "invalid_arguments" {
			t.Errorf("wrong-scope token = %s", kind)
		}
	}
	f.history.refuse(status.Error(codes.PermissionDenied, "test denied"))
	if kind := f.attempt(t, f.caller, contract.ActionReadTurns, turnArguments(t, sandbox, session, map[string]any{"count": 1})).failure(); kind != "read_failed" {
		t.Errorf("denied = %s", kind)
	}
	// The same Sandbox name after replacement is never enough, even when the Session evidence matches.
	sandbox.UID = "00000000-0000-0000-0000-000000000000"
	if kind := f.attempt(t, f.caller, contract.ActionReadTurns, turnArguments(t, sandbox, session, map[string]any{"count": 1})).failure(); kind != "sandbox_replaced" {
		t.Errorf("replaced = %s", kind)
	}
}

// waitHistoryClient overrides only the RPC boundary a deadline test controls.
type waitHistoryClient struct {
	historypb.HistoryServiceClient
	summary func(context.Context, *historypb.GetSessionSummaryRequest, ...grpc.CallOption) (*historypb.SessionSummary, error)
	follow  func(context.Context, *historypb.FollowSessionRequest, ...grpc.CallOption) (grpc.ServerStreamingClient[historypb.FollowSessionResponse], error)
}

func (h *waitHistoryClient) GetSessionSummary(ctx context.Context, request *historypb.GetSessionSummaryRequest, opts ...grpc.CallOption) (*historypb.SessionSummary, error) {
	if h.summary != nil {
		return h.summary(ctx, request, opts...)
	}
	return h.HistoryServiceClient.GetSessionSummary(ctx, request, opts...)
}

func (h *waitHistoryClient) FollowSession(ctx context.Context, request *historypb.FollowSessionRequest, opts ...grpc.CallOption) (grpc.ServerStreamingClient[historypb.FollowSessionResponse], error) {
	return h.follow(ctx, request, opts...)
}

func TestWaitAuthorizationRetainsReadBudget(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-wait-authorization")
	session := uuid.NewString()
	f.history.hold(summaryOf(f, session, sandbox, nil))
	parent, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	deadline, _ := parent.Deadline()
	original := f.executor.history
	f.executor.history = &waitHistoryClient{
		HistoryServiceClient: original,
		summary: func(ctx context.Context, request *historypb.GetSessionSummaryRequest, opts ...grpc.CallOption) (*historypb.SessionSummary, error) {
			if got, _ := ctx.Deadline(); !got.Equal(deadline) {
				t.Errorf("authorization deadline = %v; want caller budget %v", got, deadline)
			}
			return original.GetSessionSummary(ctx, request, opts...)
		},
		follow: func(ctx context.Context, _ *historypb.FollowSessionRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[historypb.FollowSessionResponse], error) {
			if got, _ := ctx.Deadline(); !got.Before(deadline) {
				t.Errorf("follow deadline = %v; want requested wait before %v", got, deadline)
			}
			<-ctx.Done()
			return nil, status.Error(codes.DeadlineExceeded, "test follow open deadline")
		},
	}
	got, err := f.executor.waitForTurn(parent, contract.WaitForTurnArgs{Sandbox: sandbox, SessionID: session, ResumeToken: resumeToken(sandbox, session, 2), WaitSeconds: 0.01})
	if err != nil || got.Completed || got.ResumeToken != resumeToken(sandbox, session, 2) {
		t.Fatalf("wait = %+v, %v", got, err)
	}
}

type reconnectAtDeadline struct {
	grpc.ClientStream
	ctx context.Context
}

func (s *reconnectAtDeadline) Recv() (*historypb.FollowSessionResponse, error) {
	<-s.ctx.Done()
	return &historypb.FollowSessionResponse{ThroughCursor: 9, Frame: &historypb.FollowSessionResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}}, nil
}

func TestWaitDoesNotReopenAfterDeadline(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-no-late-reconnect")
	session := uuid.NewString()
	f.history.hold(summaryOf(f, session, sandbox, nil))
	calls := 0
	f.executor.history = &waitHistoryClient{
		HistoryServiceClient: f.executor.history,
		follow: func(ctx context.Context, _ *historypb.FollowSessionRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[historypb.FollowSessionResponse], error) {
			calls++
			if calls > 1 {
				return nil, status.Error(codes.DeadlineExceeded, "test reopened after deadline")
			}
			return &reconnectAtDeadline{ctx: ctx}, nil
		},
	}
	got, err := f.executor.waitForTurn(t.Context(), contract.WaitForTurnArgs{Sandbox: sandbox, SessionID: session, ResumeToken: resumeToken(sandbox, session, 2), WaitSeconds: 0.01})
	if err != nil || got.Completed || got.ResumeToken != resumeToken(sandbox, session, 9) || calls != 1 {
		t.Fatalf("wait = %+v, %v; follow calls = %d", got, err, calls)
	}
}

func TestWaitKeepsAuthorizationCallerAndBackendFailures(t *testing.T) {
	for _, failure := range []string{"authorization_deadline", "caller_cancelled", "backend_unavailable", "backend_deadline"} {
		t.Run(failure, func(t *testing.T) {
			f := newFixture(t)
			sandbox := f.launch(t, "test-wait-failure")
			session := uuid.NewString()
			f.history.hold(summaryOf(f, session, sandbox, nil))
			parent, cancel := context.WithCancel(t.Context())
			defer cancel()
			original := f.executor.history
			f.executor.history = &waitHistoryClient{
				HistoryServiceClient: original,
				summary: func(ctx context.Context, request *historypb.GetSessionSummaryRequest, opts ...grpc.CallOption) (*historypb.SessionSummary, error) {
					if failure == "authorization_deadline" {
						return nil, status.Error(codes.DeadlineExceeded, "test authorization deadline")
					}
					return original.GetSessionSummary(ctx, request, opts...)
				},
				follow: func(_ context.Context, _ *historypb.FollowSessionRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[historypb.FollowSessionResponse], error) {
					if failure == "authorization_deadline" {
						t.Error("follow called after failed authorization")
					}
					if failure == "caller_cancelled" {
						cancel()
						return nil, status.Error(codes.DeadlineExceeded, "test caller ended")
					}
					if failure == "backend_deadline" {
						return nil, status.Error(codes.DeadlineExceeded, "test internal History timeout")
					}
					return nil, status.Error(codes.Unavailable, "test backend failure")
				},
			}
			_, err := f.executor.waitForTurn(parent, contract.WaitForTurnArgs{Sandbox: sandbox, SessionID: session, ResumeToken: resumeToken(sandbox, session, 2), WaitSeconds: 30})
			var refused *refusal
			if !errors.As(err, &refused) || refused.kind != "read_failed" {
				t.Fatalf("wait failure = %v; want read_failed", err)
			}
		})
	}
}

func TestReadTurnsPreservesCommandOriginsWithoutReplayingInputs(t *testing.T) {
	f := newFixture(t)
	sandbox := f.launch(t, "test-input-origins")
	session := uuid.NewString()
	f.history.hold(summaryOf(f, session, sandbox, nil))
	f.history.follow = func(_ *historypb.FollowSessionRequest, stream grpc.ServerStreamingServer[historypb.FollowSessionResponse]) error {
		confirmation := &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{TurnId: "test-turn", HarnessMessageId: "test-message", Text: "same text", OriginCommandIds: []string{"test-command-a", "test-command-b"}}}}
		frame := turnEntries(
			2,
			&eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "test-turn"}}},
			confirmation,
		)
		for range 2 {
			if err := stream.Send(frame); err != nil {
				return err
			}
		}
		if err := stream.Send(turnEntries(
			4, confirmation,
			&eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{TurnId: "test-turn", HarnessMessageId: "test-unattributed", Text: "same text"}}},
		)); err != nil {
			return err
		}
		return stream.Send(&historypb.FollowSessionResponse{ThroughCursor: 4, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}})
	}
	got := resultOf[Turns](f.attempt(t, f.caller, contract.ActionReadTurns, turnArguments(t, sandbox, session, map[string]any{"count": 1})))
	want := []TurnInput{{Text: "same text", CommandIDs: []string{"test-command-a", "test-command-b"}}, {Text: "same text", CommandIDs: []string{}}}
	if len(got.Turns) != 1 || !reflect.DeepEqual(got.Turns[0].Input, want) {
		t.Fatalf("turns = %+v; want input %+v", got.Turns, want)
	}
}
