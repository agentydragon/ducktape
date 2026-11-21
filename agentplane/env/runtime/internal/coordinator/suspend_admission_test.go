package coordinator

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/net/peertls"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestSuspendAdmissionFencePersistsAcrossIndependentWatchAndGeneration(t *testing.T) {
	workspace := t.TempDir()
	r, peers := startControl(t, lifecycleConfig(t, "http://127.0.0.1", workspace, nil, "/bin/false"), environmentruntimetransport.Insecure)
	conn := peers.dial(t, "")
	client := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	host := harnesshostpb.NewHarnessHostClient(conn)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	bindTestIncarnation(t, client, "sandbox-uid", "pod-uid-1")
	watch := func() *environmentruntimepb.WatchStateResponse {
		t.Helper()
		stream, err := client.WatchState(ctx, &environmentruntimepb.WatchStateRequest{
			SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid-1",
		})
		if err != nil {
			t.Fatal(err)
		}
		response, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		return response
	}
	call := func() context.Context { return fencedContext(ctx, "sandbox-uid", "pod-uid-1", r.bootID) }
	first := watch()
	if first.GetFacts().GetIdleSince() == nil {
		t.Fatalf("initial Runtime was not idle: %v", first.GetFacts())
	}
	idle := first.GetFacts().GetIdleSince()
	prepare := func(generation int64, since *timestamppb.Timestamp) error {
		_, err := client.PrepareSuspend(call(), &environmentruntimepb.PrepareSuspendRequest{Generation: generation, IfIdleSince: since})
		return err
	}
	resume := func(generation int64) error {
		_, err := client.ResumeAdmission(call(), &environmentruntimepb.ResumeAdmissionRequest{Generation: generation})
		return err
	}
	if err := prepare(1, timestamppb.New(idle.AsTime().Add(time.Second))); status.Code(err) != codes.Aborted {
		t.Fatalf("changed idle period PrepareSuspend = %v, want Aborted", err)
	}
	r.mu.Lock()
	r.activeAdmissions++
	r.mu.Unlock()
	if err := prepare(1, idle); status.Code(err) != codes.Aborted {
		t.Fatalf("active admission PrepareSuspend = %v, want Aborted", err)
	}
	r.mu.Lock()
	r.activeAdmissions--
	r.mu.Unlock()
	if err := prepare(1, idle); err != nil {
		t.Fatalf("idle PrepareSuspend: %v", err)
	}
	if err := prepare(1, idle); err != nil {
		t.Fatalf("same prepared generation did not replay: %v", err)
	}
	if _, err := client.GetStatus(call(), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus while prepared = %v, want Unavailable", err)
	}
	request := openLifecycleSession("suspend-adapter-session", filepath.Join(workspace, "project"))
	if _, err := host.CreateSession(call(), &harnesshostpb.CreateSessionRequest{
		SessionId: request.SessionId, Spec: request.Spec, ExecutionIdentity: request.ExecutionIdentity,
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("HarnessHost CreateSession while prepared = %v, want Unavailable", err)
	}
	if _, err := host.SubmitInput(call(), &harnesshostpb.SubmitInputRequest{
		SessionId: "not-yet-created", CommandId: "suspend-race-command", Text: "hello",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("HarnessHost SubmitInput while prepared = %v, want Unavailable before store lookup", err)
	}
	if _, err := host.ChangeModel(call(), &harnesshostpb.ChangeModelRequest{
		SessionId: "not-yet-created", CommandId: "suspend-model", Model: "model-next",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("HarnessHost ChangeModel while prepared = %v, want Unavailable before store lookup", err)
	}
	if _, err := host.ChangeReasoningEffort(call(), &harnesshostpb.ChangeReasoningEffortRequest{
		SessionId: "not-yet-created", CommandId: "suspend-effort", Effort: "high",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("HarnessHost ChangeReasoningEffort while prepared = %v, want Unavailable before store lookup", err)
	}
	if _, err := host.InterruptTurn(call(), &harnesshostpb.InterruptTurnRequest{
		SessionId: "not-yet-created", CommandId: "suspend-interrupt", TurnId: "turn-id",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("HarnessHost InterruptTurn while prepared = %v, want Unavailable before store lookup", err)
	}
	if watch, err := host.WatchSessions(call(), &harnesshostpb.WatchSessionsRequest{}); err != nil {
		t.Fatalf("start HarnessHost WatchSessions while prepared: %v", err)
	} else if _, err := watch.Recv(); status.Code(err) != codes.Unavailable {
		t.Fatalf("HarnessHost WatchSessions while prepared = %v, want Unavailable", err)
	}
	if _, err := executorpb.NewExecutorClient(conn).StartProcess(call(), &executorpb.StartProcessRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Execution while prepared = %v, want Unavailable", err)
	}
	_ = watch() // An independent same-incarnation observer does not reopen the suspend gate.
	if _, err := client.GetStatus(call(), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus after independent observation = %v, want Unavailable", err)
	}
	if err := resume(1); err != nil {
		t.Fatalf("ResumeAdmission after independent observation: %v", err)
	}
	if err := resume(1); err != nil {
		t.Fatalf("same generation ResumeAdmission did not replay: %v", err)
	}
	if err := prepare(1, idle); status.Code(err) != codes.Aborted {
		t.Fatalf("late PrepareSuspend after release = %v, want Aborted", err)
	}
	if _, err := client.GetStatus(call(), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("admission did not reopen: %v", err)
	}
	if _, err := host.SubmitInput(call(), &harnesshostpb.SubmitInputRequest{
		SessionId: "not-yet-created", CommandId: "after-resume", Text: "hello",
	}); status.Code(err) != codes.NotFound {
		t.Fatalf("HarnessHost SubmitInput after resume = %v, want NotFound from store lookup", err)
	}
	facts, err := r.facts()
	if err != nil || facts.IdleSince == nil {
		t.Fatalf("new idle period = %v, %v", facts, err)
	}
	if err := prepare(2, facts.IdleSince); err != nil {
		t.Fatalf("next generation PrepareSuspend: %v", err)
	}
	if err := resume(1); status.Code(err) != codes.Aborted {
		t.Fatalf("old ResumeAdmission reopened successor = %v, want Aborted", err)
	}
	if _, err := client.GetStatus(call(), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("old ResumeAdmission did not preserve gate: %v", err)
	}
	if err := resume(2); err != nil {
		t.Fatalf("release successor: %v", err)
	}
	facts, err = r.facts()
	if err != nil || facts.IdleSince == nil {
		t.Fatalf("idle before teardown = %v, %v", facts, err)
	}
	if err := prepare(3, facts.IdleSince); err != nil {
		t.Fatalf("prepare before teardown: %v", err)
	}
	if _, err := client.Quiesce(call(), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatalf("Quiesce while suspend gate held: %v", err)
	}
	r.mu.Lock()
	prepared, admissionOpen := r.suspendPrepared, r.admissionOpen
	r.mu.Unlock()
	if prepared || admissionOpen {
		t.Fatalf("Quiesce left suspension gate=%t, lifecycle admission=%t", prepared, admissionOpen)
	}
}
