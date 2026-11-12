package retained

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"testing"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

func TestSessionRecordsUseCOSIState(t *testing.T) {
	root := t.TempDir()
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	store, err := NewStore(root, resources)
	if err != nil {
		t.Fatal(err)
	}
	budget := int64(32768)
	native := "native-session"
	wantScript := []byte("printf 'exact \\t'\n")
	record := &Record{Placement: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}, Cwd: "/workspace", InitialModel: "route/model", TotalContextBudgetTokens: &budget, SetupScript: bytes.Clone(wantScript), EventSourceId: "source", NativeSessionId: &native, ExecutionIdentity: &identitypb.Identity{Name: "identity", Uid: "uid"}}
	if err := store.Write("session-1", record); err != nil {
		t.Fatal(err)
	}
	budget = 1
	native = "changed"
	record.SetupScript[0] = 'X'
	record.ExecutionIdentity.Name = "changed"
	read, err := store.Read("session-1")
	if err != nil {
		t.Fatal(err)
	}
	if *read.TotalContextBudgetTokens != 32768 || *read.NativeSessionId != "native-session" || !bytes.Equal(read.SetupScript, wantScript) || read.ExecutionIdentity.GetName() != "identity" {
		t.Fatalf("record = %+v", read)
	}
	*read.NativeSessionId = "mutated"
	read.SetupScript[0] = 'X'
	read.ExecutionIdentity.Name = "mutated"
	again, err := store.Read("session-1")
	if err != nil || *again.NativeSessionId != "native-session" || !bytes.Equal(again.SetupScript, wantScript) || again.ExecutionIdentity.GetName() != "identity" {
		t.Fatalf("mutated resource: %+v, %v", again, err)
	}
	ids, err := store.SessionIDs()
	if err != nil || len(ids) != 1 || ids[0] != "session-1" {
		t.Fatalf("IDs = %v, %v", ids, err)
	}
	if _, err := os.Stat(filepath.Join(root, "session-1", "session.json")); !os.IsNotExist(err) {
		t.Fatalf("unexpected Session JSON file: %v", err)
	}
	legacyDir := filepath.Join(root, "unregistered")
	if err := os.Mkdir(legacyDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(legacyDir, "session.json"), []byte(`{"harness":"HARNESS_CLAUDE"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if exists, err := store.Exists("unregistered"); err != nil || exists {
		t.Fatalf("file was accepted as a Session: %t, %v", exists, err)
	}
	ids, err = store.SessionIDs()
	if err != nil || len(ids) != 1 || ids[0] != "session-1" {
		t.Fatalf("file affected Session IDs: %v, %v", ids, err)
	}
}

func TestNativeIdentityUpdatePreservesReservationThroughRestart(t *testing.T) {
	root := t.TempDir()
	resources, backing, err := cosistate.Open(root, lifecycle.Namespace, SessionRecordRegistration())
	if err != nil {
		t.Fatal(err)
	}
	store, err := NewStore(filepath.Join(root, "sessions"), resources)
	if err != nil {
		t.Fatal(err)
	}
	record := &Record{Placement: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}, Cwd: "/workspace", InitialModel: "old-model", InitialReasoningEffort: "old-effort", EventSourceId: "source"}
	unknown := []byte{0xa0, 0x06, 0x07} // Field 100, varint 7.
	record.ProtoReflect().SetUnknown(unknown)
	if err := store.Write("session", record); err != nil {
		t.Fatal(err)
	}
	if err := store.UpdateNativeSessionID("session", "native-session"); err != nil {
		t.Fatal(err)
	}
	if err := backing.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, reopenedBacking, err := cosistate.Open(root, lifecycle.Namespace, SessionRecordRegistration())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = reopenedBacking.Close() })
	restored, err := NewStore(filepath.Join(root, "sessions"), reopened)
	if err != nil {
		t.Fatal(err)
	}
	restoredRecord, err := restored.Read("session")
	if err != nil {
		t.Fatal(err)
	}
	if restoredRecord.InitialModel != "old-model" || restoredRecord.InitialReasoningEffort != "old-effort" || restoredRecord.NativeSessionId == nil || *restoredRecord.NativeSessionId != "native-session" || restoredRecord.EventSourceId != "source" || !bytes.Equal(restoredRecord.ProtoReflect().GetUnknown(), unknown) {
		t.Fatalf("restored concurrent updates = %+v", restoredRecord)
	}
}

func TestSessionFieldUpdateDoesNotCreateRecord(t *testing.T) {
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	store, err := NewStore(t.TempDir(), resources)
	if err != nil {
		t.Fatal(err)
	}
	var storageErr *StorageError
	if err := store.UpdateNativeSessionID("missing", "native"); !errors.As(err, &storageErr) || !state.IsNotFoundError(err) {
		t.Fatalf("missing update = %v, want storage not-found error", err)
	}
	if exists, err := store.Exists("missing"); err != nil || exists {
		t.Fatalf("missing record created: exists=%t, err=%v", exists, err)
	}
}

func TestIngressAcknowledgementsUsePersistentCOSIAndIgnoreLegacyJSON(t *testing.T) {
	root := t.TempDir()
	sessionsRoot := filepath.Join(root, "sessions")
	legacyDirectory := filepath.Join(sessionsRoot, "session-1")
	if err := os.MkdirAll(legacyDirectory, 0o700); err != nil {
		t.Fatal(err)
	}
	legacyJSON := []byte(`{"cursor":777,"sealed":true}`)
	legacyPath := filepath.Join(legacyDirectory, "acknowledgement.json")
	if err := os.WriteFile(legacyPath, legacyJSON, 0o600); err != nil {
		t.Fatal(err)
	}
	stateDir := filepath.Join(root, "state")
	if err := os.Mkdir(stateDir, 0o700); err != nil {
		t.Fatal(err)
	}

	resources, backing, err := cosistate.Open(stateDir, lifecycle.Namespace, SessionRecordRegistration(), IngressAckRegistration())
	if err != nil {
		t.Fatal(err)
	}
	store, err := NewStore(sessionsRoot, resources)
	if err != nil {
		t.Fatal(err)
	}
	if ack, err := store.ReadAcknowledgement("session-1"); err != nil || ack != nil {
		t.Fatalf("legacy file acknowledgement = %+v, err=%v; want no COSI state", ack, err)
	}
	sealed := Acknowledgement{Cursor: 12, Sealed: true}
	if err := store.WriteAcknowledgement("session-1", sealed); err != nil {
		t.Fatal(err)
	}
	if err := backing.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, reopenedBacking, err := cosistate.Open(stateDir, lifecycle.Namespace, SessionRecordRegistration(), IngressAckRegistration())
	if err != nil {
		t.Fatal(err)
	}
	reopenedStore, err := NewStore(sessionsRoot, reopened)
	if err != nil {
		t.Fatal(err)
	}
	got, err := reopenedStore.ReadAcknowledgement("session-1")
	if err != nil || got == nil || *got != sealed {
		t.Fatalf("reopened sealed COSI acknowledgement = %+v, err=%v; want %+v", got, err, sealed)
	}
	unsealed := sealed
	unsealed.Sealed = false
	if err := reopenedStore.WriteAcknowledgement("session-1", unsealed); err != nil {
		t.Fatal(err)
	}
	if err := reopenedBacking.Close(); err != nil {
		t.Fatal(err)
	}

	reopenedAgain, reopenedAgainBacking, err := cosistate.Open(stateDir, lifecycle.Namespace, SessionRecordRegistration(), IngressAckRegistration())
	if err != nil {
		t.Fatal(err)
	}
	defer reopenedAgainBacking.Close()
	unsealedStore, err := NewStore(sessionsRoot, reopenedAgain)
	if err != nil {
		t.Fatal(err)
	}
	got, err = unsealedStore.ReadAcknowledgement("session-1")
	if err != nil || got == nil || *got != unsealed {
		t.Fatalf("reopened unsealed COSI acknowledgement = %+v, err=%v; want %+v", got, err, unsealed)
	}
	if got, err := os.ReadFile(legacyPath); err != nil || !bytes.Equal(got, legacyJSON) {
		t.Fatalf("legacy file changed: %q, err=%v", got, err)
	}
}
