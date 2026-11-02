package main

import (
	"encoding/json"
	"testing"
	"time"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func sandboxString(value string) *string { return &value }

func TestSandboxViewKeepsBrowserFieldNamesAndDerivedValues(t *testing.T) {
	sessionID := "session-a"
	status, err := structpb.NewStruct(map[string]any{"conditions": []any{}, "ready": true})
	if err != nil {
		t.Fatal(err)
	}
	value := &sandboxespb.Sandbox{
		Name: "worker", Uid: "sandbox-uid", Namespace: "sandboxes", CreatedAt: timestamppb.New(time.Date(2026, 10, 1, 2, 3, 4, 0, time.UTC)),
		OperatingMode: sandboxString("Running"), Status: status,
		ServiceAccount: &sandboxespb.ServiceAccount{Namespace: "sandboxes", Name: "worker"},
		Binding:        &sandboxespb.SandboxBinding{Bootstrap: "echo ready"},
		Preset:         sandboxString("Default"), WakeOnSend: true, IdleSuspendAfter: durationpb.New(90 * time.Second),
		IdleSuspendAt: timestamppb.New(time.Date(2026, 10, 1, 2, 5, 4, 0, time.UTC)),
		SuspendedBy:   sandboxString("Idle"), Phase: sandboxespb.SandboxPhase_SANDBOX_PHASE_RUNNING,
		AutosuspendBlockers: []*sandboxespb.AutosuspendBlocker{
			{Reason: "ActiveTurn", SessionId: &sessionID},
			{Reason: "Bootstrap"},
		},
		Pod: &sandboxespb.SandboxPod{
			Name: "worker-pod", Namespace: "sandboxes", Uid: "pod-uid",
			OwnerReferences: []*sandboxespb.OwnerReference{{ApiVersion: "agentplane.allegedly.works/v1alpha1", Kind: "Sandbox", Name: "worker", Uid: "sandbox-uid", Controller: true}},
		},
	}
	view := sandboxView(value)
	if view.Name != "worker" || view.Phase != "Running" || view.IdleSuspendAfterSeconds == nil || *view.IdleSuspendAfterSeconds != 90 {
		t.Fatalf("unexpected Sandbox view: %+v", view)
	}
	if view.Pod == nil || view.Pod.OwnerReferences[0].APIVersion != "agentplane.allegedly.works/v1alpha1" {
		t.Fatalf("unexpected Pod view: %+v", view.Pod)
	}
	body, err := json.Marshal(view)
	if err != nil {
		t.Fatal(err)
	}
	var object map[string]any
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"created_at", "operating_mode", "service_account", "idle_suspend_after_seconds", "suspended_by"} {
		if _, ok := object[key]; !ok {
			t.Errorf("browser JSON lacks %q: %s", key, body)
		}
	}
	if object["idle_suspend_at"] != "2026-10-01T02:05:04Z" {
		t.Fatalf("browser idle deadline = %v", object["idle_suspend_at"])
	}
	blockers, ok := object["autosuspend_blockers"].([]any)
	if !ok || len(blockers) != 2 {
		t.Fatalf("browser autosuspend blockers = %v, want two rows", object["autosuspend_blockers"])
	}
	firstBlocker, ok := blockers[0].(map[string]any)
	if !ok || firstBlocker["reason"] != "ActiveTurn" || firstBlocker["session_id"] != sessionID {
		t.Fatalf("session-scoped browser blocker = %v", blockers[0])
	}
	secondBlocker, ok := blockers[1].(map[string]any)
	if !ok || secondBlocker["reason"] != "Bootstrap" || secondBlocker["session_id"] != nil {
		t.Fatalf("global browser blocker = %v", blockers[1])
	}
	if _, ok := object["createdAt"]; ok {
		t.Fatalf("browser JSON unexpectedly uses protobuf camelCase: %s", body)
	}
}

func TestSandboxViewSerializesEmptyAutosuspendBlockersAsEmptyArray(t *testing.T) {
	body, err := json.Marshal(sandboxView(&sandboxespb.Sandbox{}))
	if err != nil {
		t.Fatal(err)
	}
	var object map[string]any
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatal(err)
	}
	if blockers, ok := object["autosuspend_blockers"].([]any); !ok || len(blockers) != 0 {
		t.Fatalf("empty browser autosuspend blockers = %v, want []", object["autosuspend_blockers"])
	}
}

func TestSandboxViewProjectsEveryPhaseAndAbsentMode(t *testing.T) {
	phases := map[sandboxespb.SandboxPhase]string{
		sandboxespb.SandboxPhase_SANDBOX_PHASE_UNSPECIFIED:  "Unspecified",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_CREATING:     "Creating",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_RUNNING:      "Running",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDING:   "Suspending",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDED:    "Suspended",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_QUIESCING:    "Quiescing",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_TEARING_DOWN: "Tearing down",
	}
	if len(phases) != len(sandboxespb.SandboxPhase_name) {
		t.Fatal("every service phase needs a browser projection")
	}
	for phase, want := range phases {
		t.Run(want, func(t *testing.T) {
			view := sandboxView(&sandboxespb.Sandbox{Phase: phase})
			if view.Phase != want || view.OperatingMode != nil {
				t.Fatalf("Sandbox projection = %+v", view)
			}
			body, err := json.Marshal(view)
			if err != nil {
				t.Fatal(err)
			}
			var object map[string]any
			if err := json.Unmarshal(body, &object); err != nil {
				t.Fatal(err)
			}
			if mode, ok := object["operating_mode"]; !ok || mode != nil {
				t.Fatalf("absent operating mode must be JSON null: %s", body)
			}
		})
	}
}
