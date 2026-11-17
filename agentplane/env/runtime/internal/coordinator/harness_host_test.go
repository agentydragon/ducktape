package coordinator

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/retained"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type harnessHostWatchStream struct {
	grpc.ServerStream
	ctx       context.Context
	responses chan *harnesshostpb.WatchSessionsResponse
}

func newHarnessHostWatchStream(ctx context.Context) *harnessHostWatchStream {
	return &harnessHostWatchStream{ctx: ctx, responses: make(chan *harnesshostpb.WatchSessionsResponse, 16)}
}

func (s *harnessHostWatchStream) Context() context.Context { return s.ctx }

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
	r := newSessionTestRuntime(t, lifecycleConfig(t, modelConfigServer(t).URL, workspace, nil, "/bin/false"))
	host := &harnessHost{runtime: r}
	callCtx, cancelCall := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancelCall()
	ctx := sessionCallContext(r, callCtx)

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
	if _, err := host.ListSessions(context.Background(), &harnesshostpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unauthorized ListSessions = %v, want Unauthenticated", err)
	}
	unauthorizedStream := newHarnessHostWatchStream(context.Background())
	if err := host.WatchSessions(&harnesshostpb.WatchSessionsRequest{}, unauthorizedStream); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unauthorized HarnessHost WatchSessions = %v, want Unauthenticated", err)
	}

	s := r.sessions.Get(request.SessionId)
	configurationCommand := &commandpb.Command{
		CommandId: "host-model-event",
		Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "confirmed-model"}},
	}
	if _, _, err := s.EventJournal().Admit(ctx, configurationCommand, ""); err != nil {
		t.Fatalf("prepare model confirmation: %v", err)
	}

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
	if caughtUp := nextHarnessHostUpdate(t, stream); caughtUp.GetCaughtUp() == nil {
		t.Fatalf("initial HarnessHost watch boundary = %v, want CaughtUp", caughtUp)
	}

	if err := s.ModelChanged(ctx, configurationCommand.CommandId, "confirmed-model", nil); err != nil {
		t.Fatalf("record ModelChanged after watch snapshot: %v", err)
	}
	modelCursor := s.EventJournal().LastCursor()
	for {
		update := nextHarnessHostUpdate(t, stream)
		if update.GetUpsert() == nil || update.GetUpsert().GetSpec().GetModel() != "confirmed-model" || update.GetUpsert().GetLastCursor() < modelCursor {
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
