package runtime

import (
	"context"
	"testing"
	"time"

	runtimecontrol "github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestSuspendAdmissionFencesWorkAcrossWatchReconnectAndGeneration(t *testing.T) {
	workspace := t.TempDir()
	config := lifecycleConfig(t, "http://127.0.0.1", workspace, nil, "/bin/false")
	r := newLifecycleRuntime(t, config)
	listener, server, err := r.control.Listen(r.executorProxy)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); _ = listener.Close() })
	credentials, err := (environmentruntimetransport.Security{Mode: environmentruntimetransport.Insecure}).ClientCredentials("")
	if err != nil {
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(credentials))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	client := environmentruntimepb.NewEnvironmentRuntimeClient(conn)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	watch := func(epoch uint64) *environmentruntimepb.WatchStateResponse {
		t.Helper()
		stream, err := client.WatchState(ctx, &environmentruntimepb.WatchStateRequest{
			SandboxUid: "sandbox-uid", IncarnationUid: "pod-uid-1", ConnectionEpoch: epoch,
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
	call := func(epoch uint64) context.Context {
		return metadata.AppendToOutgoingContext(
			ctx,
			runtimecontrol.SandboxMetadata, "sandbox-uid",
			runtimecontrol.IncarnationMetadata, "pod-uid-1",
			runtimecontrol.BootMetadata, r.bootID,
			runtimecontrol.EpochMetadata, uint64String(epoch),
		)
	}
	first := watch(1)
	if first.GetFacts().GetIdleSince() == nil {
		t.Fatalf("initial Runtime was not idle: %v", first.GetFacts())
	}
	idle := first.GetFacts().GetIdleSince()
	prepare := func(epoch uint64, generation int64, since *timestamppb.Timestamp) error {
		_, err := client.PrepareSuspend(call(epoch), &environmentruntimepb.PrepareSuspendRequest{Generation: generation, IfIdleSince: since})
		return err
	}
	resume := func(epoch uint64, generation int64) error {
		_, err := client.ResumeAdmission(call(epoch), &environmentruntimepb.ResumeAdmissionRequest{Generation: generation})
		return err
	}
	if err := prepare(1, 1, timestamppb.New(idle.AsTime().Add(time.Second))); status.Code(err) != codes.Aborted {
		t.Fatalf("changed idle period PrepareSuspend = %v, want Aborted", err)
	}
	r.mu.Lock()
	r.activeAdmissions++
	r.mu.Unlock()
	if err := prepare(1, 1, idle); status.Code(err) != codes.Aborted {
		t.Fatalf("active admission PrepareSuspend = %v, want Aborted", err)
	}
	r.mu.Lock()
	r.activeAdmissions--
	r.mu.Unlock()
	if err := prepare(1, 1, idle); err != nil {
		t.Fatalf("idle PrepareSuspend: %v", err)
	}
	if err := prepare(1, 1, idle); err != nil {
		t.Fatalf("same prepared generation did not replay: %v", err)
	}
	if _, err := client.GetStatus(call(1), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus while prepared = %v, want Unavailable", err)
	}
	if _, err := client.SubmitInput(call(1), &environmentruntimepb.SubmitInputRequest{
		SessionId: "not-yet-created", CommandId: "suspend-race-command", Text: "hello",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("SubmitInput while prepared = %v, want Unavailable before store lookup", err)
	}
	if _, err := executorpb.NewExecutorClient(conn).StartProcess(call(1), &executorpb.StartProcessRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("Execution while prepared = %v, want Unavailable", err)
	}
	_ = watch(2) // Same-incarnation reconnect must not reopen the RAM-preserved gate.
	if _, err := client.GetStatus(call(2), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus after reconnect = %v, want Unavailable", err)
	}
	if err := resume(1, 1); status.Code(err) != codes.Unavailable {
		t.Fatalf("old-epoch ResumeAdmission = %v, want Unavailable", err)
	}
	if err := resume(2, 1); err != nil {
		t.Fatalf("ResumeAdmission after verified reconnect: %v", err)
	}
	if err := resume(2, 1); err != nil {
		t.Fatalf("same generation ResumeAdmission did not replay: %v", err)
	}
	if err := prepare(2, 1, idle); status.Code(err) != codes.Aborted {
		t.Fatalf("late PrepareSuspend after release = %v, want Aborted", err)
	}
	if _, err := client.GetStatus(call(2), &sessionpb.GetStatusRequest{}); err != nil {
		t.Fatalf("admission did not reopen: %v", err)
	}
	facts, err := r.facts()
	if err != nil || facts.IdleSince == nil {
		t.Fatalf("new idle period = %v, %v", facts, err)
	}
	if err := prepare(2, 2, facts.IdleSince); err != nil {
		t.Fatalf("next generation PrepareSuspend: %v", err)
	}
	if err := resume(2, 1); status.Code(err) != codes.Aborted {
		t.Fatalf("old ResumeAdmission reopened successor = %v, want Aborted", err)
	}
	if _, err := client.GetStatus(call(2), &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("old ResumeAdmission did not preserve gate: %v", err)
	}
	if err := resume(2, 2); err != nil {
		t.Fatalf("release successor: %v", err)
	}
	facts, err = r.facts()
	if err != nil || facts.IdleSince == nil {
		t.Fatalf("idle before teardown = %v, %v", facts, err)
	}
	if err := prepare(2, 3, facts.IdleSince); err != nil {
		t.Fatalf("prepare before teardown: %v", err)
	}
	if _, err := client.Quiesce(call(2), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatalf("Quiesce while suspend gate held: %v", err)
	}
	r.mu.Lock()
	prepared, admissionOpen := r.suspendPrepared, r.admissionOpen
	r.mu.Unlock()
	if prepared || admissionOpen {
		t.Fatalf("Quiesce left suspension gate=%t, lifecycle admission=%t", prepared, admissionOpen)
	}
}
