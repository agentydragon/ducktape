package coordinator

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/retained"
	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type harnessHostWatchStream struct {
	grpc.ServerStream
	ctx       context.Context
	responses chan *harnesshostpb.WatchSessionsResponse
	header    metadata.MD
}

func newHarnessHostWatchStream(ctx context.Context) *harnessHostWatchStream {
	return &harnessHostWatchStream{ctx: ctx, responses: make(chan *harnesshostpb.WatchSessionsResponse, 16)}
}

func (s *harnessHostWatchStream) Context() context.Context { return s.ctx }

func (s *harnessHostWatchStream) SendHeader(header metadata.MD) error {
	s.header = header.Copy()
	return nil
}

type harnessHostTransport struct{ header metadata.MD }

func (*harnessHostTransport) Method() string {
	return harnesshostpb.HarnessHost_ListSessions_FullMethodName
}

func (s *harnessHostTransport) SetHeader(header metadata.MD) error {
	s.header = metadata.Join(s.header, header)
	return nil
}
func (s *harnessHostTransport) SendHeader(header metadata.MD) error { return s.SetHeader(header) }
func (*harnessHostTransport) SetTrailer(metadata.MD) error          { return nil }

func (s *harnessHostWatchStream) Send(response *harnesshostpb.WatchSessionsResponse) error {
	select {
	case s.responses <- response:
		return nil
	case <-s.ctx.Done():
		return s.ctx.Err()
	}
}

func nextHarnessHostUpdate(t *testing.T, stream *harnessHostWatchStream) *harnesshostpb.WatchSessionsResponse {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	select {
	case response := <-stream.responses:
		return response
	case <-ctx.Done():
		t.Fatalf("waiting for HarnessHost watch update: %v", ctx.Err())
		return nil
	}
}

func TestHarnessHostCommandsAndCurrentStateWatch(t *testing.T) {
	workspace := t.TempDir()
	r := newSessionTestRuntime(t, lifecycleConfig(t, workspace, nil, "/bin/false"))
	host := &harnessHost{runtime: r}
	callCtx, cancelCall := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancelCall()
	transport := &harnessHostTransport{}
	ctx := grpc.NewContextWithServerTransportStream(sessionCallContext(r, callCtx), transport)

	request := openLifecycleSession("harness-host", filepath.Join(workspace, "project"))
	created, err := host.CreateSession(ctx, &harnesshostpb.CreateSessionRequest{
		SessionId: request.SessionId, Spec: request.Spec, SetupScript: request.SetupScript,
		ExecutionIdentity: request.ExecutionIdentity,
	})
	if err != nil || created.GetAttached().GetSessionId() != request.SessionId {
		t.Fatalf("HarnessHost CreateSession = %v, %v", created, err)
	}

	listed, err := host.ListSessions(ctx, &harnesshostpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 1 || listed.GetSessions()[0].GetSessionId() != request.SessionId {
		t.Fatalf("HarnessHost ListSessions = %v, %v", listed, err)
	}
	if err := harnesshost.RequireSessionVersion(transport.header); err != nil {
		t.Fatalf("ListSessions version header: %v", err)
	}
	if _, err := host.ListSessions(context.Background(), &harnesshostpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unauthorized ListSessions = %v, want Unauthenticated", err)
	}
	unauthorizedStream := newHarnessHostWatchStream(context.Background())
	if err := host.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, unauthorizedStream); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unauthorized HarnessHost WatchSessions = %v, want Unauthenticated", err)
	}

	s := r.sessions.Get(request.SessionId)
	watchCtx, cancelWatch := context.WithCancel(ctx)
	stream := newHarnessHostWatchStream(watchCtx)
	watchDone := make(chan error, 1)
	go func() {
		watchDone <- host.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, stream)
	}()
	initial := nextHarnessHostUpdate(t, stream)
	if initial.GetUpsert().GetSessionId() != request.SessionId {
		t.Fatalf("initial HarnessHost watch update = %v, want the created Session", initial)
	}
	if err := harnesshost.RequireSessionVersion(stream.header); err != nil {
		t.Fatalf("WatchSessions version header: %v", err)
	}
	if caughtUp := nextHarnessHostUpdate(t, stream); caughtUp.GetCaughtUp() == nil {
		t.Fatalf("initial HarnessHost watch boundary = %v, want CaughtUp", caughtUp)
	}

	if err := s.Emit(ctx, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "host-turn"}}}, nil); err != nil {
		t.Fatalf("record TurnStarted after watch snapshot: %v", err)
	}
	for {
		update := nextHarnessHostUpdate(t, stream)
		if update.GetUpsert() == nil || update.GetUpsert().GetActiveTurnId() != "host-turn" {
			continue
		}
		if caughtUp := nextHarnessHostUpdate(t, stream); caughtUp.GetCaughtUp() == nil {
			t.Fatalf("changed HarnessHost watch boundary = %v, want CaughtUp", caughtUp)
		}
		break
	}

	input, err := host.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{
		SessionId: request.SessionId, CommandId: "host-input", Text: "hello",
	})
	if err != nil || input.GetAdmission() == nil {
		t.Fatalf("HarnessHost SubmitInput = %v, %v", input, err)
	}
	model, err := host.ChangeModel(ctx, &harnesshostpb.ChangeModelRequest{
		SessionId: request.SessionId, CommandId: "host-model", Model: "model-next",
	})
	if err != nil || model.GetAdmission() == nil {
		t.Fatalf("HarnessHost ChangeModel = %v, %v", model, err)
	}
	effort, err := host.ChangeReasoningEffort(ctx, &harnesshostpb.ChangeReasoningEffortRequest{
		SessionId: request.SessionId, CommandId: "host-effort", Effort: "high",
	})
	if err != nil || effort.GetAdmission() == nil {
		t.Fatalf("HarnessHost ChangeReasoningEffort = %v, %v", effort, err)
	}
	if _, err := host.InterruptTurn(ctx, &harnesshostpb.InterruptTurnRequest{
		SessionId: request.SessionId, CommandId: "host-stale-interrupt", TurnId: "missing-turn",
	}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("HarnessHost stale InterruptTurn = %v, want FailedPrecondition", err)
	}
	if record, err := s.EventJournal().GetCommand(ctx, "host-stale-interrupt"); err != nil || record != nil {
		t.Fatalf("rejected HarnessHost interrupt record = %v, %v", record, err)
	}
	cancelWatch()
	watchEndCtx, cancelWatchEnd := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancelWatchEnd()
	select {
	case err := <-watchDone:
		if err == nil {
			t.Fatal("HarnessHost watch returned without cancellation")
		}
	case <-watchEndCtx.Done():
		t.Fatalf("HarnessHost watch did not stop after cancellation: %v", watchEndCtx.Err())
	}
}

func TestHarnessHostQuiesceDrainsInFlightCommand(t *testing.T) {
	r, s := lifecycleTestSession(t)
	host := &harnessHost{runtime: r}
	ctx, cancel := context.WithTimeout(sessionCallContext(r, context.Background()), lifecycleTestTimeout)
	defer cancel()
	waitRuntime(t, r, func() bool {
		facts, err := r.facts()
		return err == nil && facts.IdleSince != nil
	}, "Session to become idle before the held command")
	facts, err := r.facts()
	if err != nil || facts.IdleSince == nil {
		t.Fatalf("idle facts before HarnessHost admission = %v, %v", facts, err)
	}

	releaseBatch := holdJournalBatch(t, ctx, s)
	firstDone := make(chan error, 1)
	go func() {
		response, err := host.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{
			SessionId: s.ID(), CommandId: "before-quiesce", Text: "accepted before quiesce",
		})
		if err == nil && response.GetAdmission() == nil {
			err = errors.New("HarnessHost SubmitInput returned no admission")
		}
		firstDone <- err
	}()
	if err := watchSessionResource(ctx, r.lifecycleState, lifecycle.EnvironmentResourceID, func(input environmentAdmissionCountSpec) (bool, error) {
		return input.Admissions == 1, nil
	}); err != nil {
		releaseBatch()
		t.Fatalf("wait for HarnessHost admission: %v", err)
	}
	if _, err := r.PrepareSuspend(ctx, &environmentruntimepb.PrepareSuspendRequest{Generation: 1, IfIdleSince: facts.IdleSince}); status.Code(err) != codes.Aborted {
		releaseBatch()
		t.Fatalf("PrepareSuspend with an in-flight HarnessHost admission = %v, want Aborted", err)
	}
	if _, err := r.Quiesce(ctx, &sessionpb.QuiesceRequest{}); err != nil {
		releaseBatch()
		t.Fatalf("accept Quiesce: %v", err)
	}
	waitLifecycle(t, r, func() bool {
		current := currentSessionLifecycleStatus(r)
		return current.Stage == lifecycle.Draining && current.Blocker.Kind == lifecycle.AdmissionsPending
	}, "Quiesce to wait for the in-flight HarnessHost admission")
	if _, err := host.CreateSession(ctx, &harnesshostpb.CreateSessionRequest{
		SessionId: "after-quiesce", Spec: s.Attached().Spec,
	}); status.Code(err) != codes.Aborted {
		releaseBatch()
		t.Fatalf("HarnessHost CreateSession after Quiesce = %v, want Aborted", err)
	}
	lateDone := make(chan error, 1)
	go func() {
		_, err := host.SubmitInput(ctx, &harnesshostpb.SubmitInputRequest{
			SessionId: s.ID(), CommandId: "after-quiesce", Text: "rejected after quiesce",
		})
		lateDone <- err
	}()
	if err := watchSessionResource(ctx, r.lifecycleState, lifecycle.EnvironmentResourceID, func(input environmentAdmissionCountSpec) (bool, error) {
		return input.Admissions == 2, nil
	}); err != nil {
		releaseBatch()
		t.Fatalf("wait for post-Quiesce HarnessHost admission: %v", err)
	}
	if cursor := s.EventJournal().LastCursor(); cursor != 0 {
		releaseBatch()
		t.Fatalf("held admission changed journal cursor before release: %d", cursor)
	}
	select {
	case err := <-firstDone:
		releaseBatch()
		t.Fatalf("Quiesce allowed the held admission to settle early: %v", err)
	default:
	}
	releaseBatch()
	// Quiesce closes the Session fence before the waiting command reaches its durable write.
	if err := <-firstDone; status.Code(err) != codes.Aborted {
		t.Fatalf("in-flight HarnessHost admission = %v, want Aborted after draining", err)
	}
	if err := <-lateDone; status.Code(err) != codes.Aborted {
		t.Fatalf("HarnessHost SubmitInput after Quiesce = %v, want Aborted", err)
	}
	if record, err := s.EventJournal().GetCommand(ctx, "after-quiesce"); err != nil || record != nil {
		t.Fatalf("post-Quiesce command record = %v, %v", record, err)
	}
	waitLifecycle(t, r, func() bool { return currentSessionLifecycleStatus(r).Stage == lifecycle.Flushing }, "Quiesce to seal the drained Session")
	seal := s.EventJournal().SealState()
	if seal == nil {
		t.Fatal("Quiesce did not seal the drained Session")
	}
	if err := r.store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool { return currentSessionLifecycleStatus(r).Stage == lifecycle.Quiesced }, "Quiesce to finish after the exact seal acknowledgement")
}
