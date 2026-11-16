package threads

import (
	"context"
	"errors"
	"testing"

	threadstore "github.com/agentydragon/ducktape/agentplane/app/internal/threads"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

type environmentReaderFake struct {
	sandbox   *environmentpb.EnvironmentDescriptor
	err       error
	exactName string
	exactUID  string
}

func (f *environmentReaderFake) GetEnvironment(_ context.Context, request *environmentpb.GetEnvironmentRequest, _ ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error) {
	target := request.GetTarget()
	f.exactName, f.exactUID = target.GetName(), target.GetUid()
	if f.err != nil {
		return nil, f.err
	}
	if f.sandbox.GetTarget().GetName() != f.exactName || f.sandbox.GetTarget().GetUid() != f.exactUID {
		return nil, errors.New("exact target UID mismatch")
	}
	return f.sandbox, nil
}

type runtimeStatusReaderFake struct {
	status *sessionpb.EnvironmentRuntimeStatus
	err    error
	target *identitypb.EnvironmentTarget
}

func (f *runtimeStatusReaderFake) EnvironmentRuntimeStatus(_ context.Context, sandbox *environmentpb.EnvironmentDescriptor) (*sessionpb.EnvironmentRuntimeStatus, error) {
	f.target = sandbox.GetTarget()
	return f.status, f.err
}

func TestThreadSandboxReadinessRequiresRunningReadyPod(t *testing.T) {
	ready := &environmentpb.EnvironmentDescriptor{
		ObservedState: &environmentpb.EnvironmentObservedState{
			OperatingMode: environmentpb.OperatingMode_OPERATING_MODE_RUNNING,
			Readiness:     &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_READY},
		},
	}
	if !threadSandboxReady(ready) {
		t.Fatal("running Sandbox with Ready=True was rejected")
	}
	if threadSandboxReady(&environmentpb.EnvironmentDescriptor{ObservedState: &environmentpb.EnvironmentObservedState{Readiness: ready.GetObservedState().GetReadiness()}}) {
		t.Fatal("Ready condition without a reported mode was accepted")
	}
	if threadSandboxReady(&environmentpb.EnvironmentDescriptor{
		ObservedState: &environmentpb.EnvironmentObservedState{
			OperatingMode: environmentpb.OperatingMode_OPERATING_MODE_SUSPENDED,
			Readiness:     ready.GetObservedState().GetReadiness(),
		},
	}) {
		t.Fatal("suspended Sandbox was accepted")
	}
}

func TestEnsureThreadArchivableRejectsBusyEnvironmentRuntime(t *testing.T) {
	id := uuid.New()
	thread := threadstore.Thread{ID: id}
	sandbox := &environmentpb.EnvironmentDescriptor{
		Target: &identitypb.EnvironmentTarget{Namespace: "team", Name: "sandbox", Uid: "sandbox-uid"},
		ObservedState: &environmentpb.EnvironmentObservedState{
			OperatingMode: environmentpb.OperatingMode_OPERATING_MODE_RUNNING,
			Readiness:     &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_READY},
		},
		KubernetesDiagnostics: &environmentpb.KubernetesDiagnostics{Pod: &environmentpb.SandboxPod{Name: "environment-runtime-pod"}},
	}
	inventory := &environmentReaderFake{sandbox: sandbox}
	environmentRuntime := &runtimeStatusReaderFake{status: &sessionpb.EnvironmentRuntimeStatus{Sessions: []*sessionpb.SessionStatus{{SessionId: id.String(), Busy: true}}}}
	summary := &historypb.SessionSummary{Sandbox: &historypb.SandboxRef{Name: "sandbox", Uid: proto.String("sandbox-uid")}}
	if err := ensureThreadArchivable(context.Background(), "tools", thread, summary, inventory, environmentRuntime); !errors.Is(err, errThreadBusy) {
		t.Fatalf("busy Environment Runtime archive check = %v, want errThreadBusy", err)
	}
	if inventory.exactName != "sandbox" || inventory.exactUID != "sandbox-uid" {
		t.Fatalf("archive lookup identity = %q/%q, want exact Sandbox UID", inventory.exactName, inventory.exactUID)
	}
	if environmentRuntime.target.GetUid() != "sandbox-uid" {
		t.Fatalf("runtime status target = %v, want exact Sandbox UID", environmentRuntime.target)
	}
	environmentRuntime.status.Sessions[0].Busy = false
	if err := ensureThreadArchivable(context.Background(), "tools", thread, summary, inventory, environmentRuntime); err != nil {
		t.Fatalf("idle Environment Runtime archive check = %v, want nil", err)
	}
	sandbox.ObservedState.OperatingMode = environmentpb.OperatingMode_OPERATING_MODE_SUSPENDED
	if err := ensureThreadArchivable(context.Background(), "tools", thread, summary, inventory, environmentRuntime); !errors.Is(err, errThreadEnvironmentRuntimeUnready) {
		t.Fatalf("unready Environment Runtime archive check = %v, want errThreadEnvironmentRuntimeUnready", err)
	}
}

func TestEnsureThreadArchivableReportsLiveBusyReason(t *testing.T) {
	id := uuid.New()
	sandbox := &environmentpb.EnvironmentDescriptor{
		Target: &identitypb.EnvironmentTarget{Namespace: "team", Name: "sandbox", Uid: "sandbox-uid"},
		ObservedState: &environmentpb.EnvironmentObservedState{
			OperatingMode: environmentpb.OperatingMode_OPERATING_MODE_RUNNING,
			Readiness:     &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_READY},
		},
		KubernetesDiagnostics: &environmentpb.KubernetesDiagnostics{Pod: &environmentpb.SandboxPod{Name: "environment-runtime-pod"}},
	}
	thread := threadstore.Thread{ID: id}
	summary := &historypb.SessionSummary{Sandbox: &historypb.SandboxRef{Name: "sandbox", Uid: proto.String("sandbox-uid")}}

	for _, test := range []struct {
		name      string
		blockers  []string
		wantError string
	}{
		{name: "active turn", blockers: []string{"ActiveTurn"}, wantError: "An active turn is still running. Wait for the reported work to finish before archiving this Thread."},
		{name: "pending command", blockers: []string{"PendingCommand"}, wantError: "A command is still pending. Wait for the reported work to finish before archiving this Thread."},
		{name: "overlapping work", blockers: []string{"ActiveTurn", "PendingCommand"}, wantError: "An active turn is still running. A command is still pending. Wait for the reported work to finish before archiving this Thread."},
		{name: "unknown busy reason", blockers: []string{"FutureReason"}, wantError: "Session work is still active. Wait for it to finish before archiving this Thread."},
	} {
		t.Run(test.name, func(t *testing.T) {
			blockers := make([]*environmentlifecyclepb.AutosuspendBlocker, 0, len(test.blockers))
			for _, reason := range test.blockers {
				sessionID := id.String()
				blockers = append(blockers, &environmentlifecyclepb.AutosuspendBlocker{Reason: reason, SessionId: &sessionID})
			}
			inventory := &environmentReaderFake{sandbox: sandbox}
			runtime := &runtimeStatusReaderFake{status: &sessionpb.EnvironmentRuntimeStatus{
				Sessions: []*sessionpb.SessionStatus{{SessionId: id.String(), Busy: true, BusyBlockers: blockers}},
			}}
			err := ensureThreadArchivable(context.Background(), "tools", thread, summary, inventory, runtime)
			if !errors.Is(err, errThreadBusy) || err.Error() != test.wantError {
				t.Fatalf("archive error = %v, want %q and errThreadBusy", err, test.wantError)
			}
		})
	}
}

func TestEnsureThreadArchivableRequiresSandboxIdentity(t *testing.T) {
	thread := threadstore.Thread{ID: uuid.New()}
	for _, summary := range []*historypb.SessionSummary{
		{Sandbox: &historypb.SandboxRef{Uid: proto.String("uid")}},
		{Sandbox: &historypb.SandboxRef{Name: "sandbox"}},
	} {
		inventory := &environmentReaderFake{sandbox: &environmentpb.EnvironmentDescriptor{
			Target: &identitypb.EnvironmentTarget{Name: "sandbox", Uid: "uid"},
		}}
		if err := ensureThreadArchivable(context.Background(), "tools", thread, summary, inventory, nil); err == nil || inventory.exactName != "" {
			t.Fatalf("archive validation for identity %+v = %v, lookup name %q; want rejection before lookup", summary, err, inventory.exactName)
		}
	}
}
