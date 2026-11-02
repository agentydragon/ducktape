package retained

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"

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
	budget := 32768
	native := "native-session"
	record := Record{Harness: sessionpb.Harness_HARNESS_CLAUDE, CWD: "/workspace", Model: "route/model", TotalContextBudgetTokens: &budget, SetupScript: "printf 'exact \\t'\n", EventSourceID: "source", NativeSessionID: &native}
	if err := store.Write("session-1", record); err != nil {
		t.Fatal(err)
	}
	budget = 1
	native = "changed"
	read, err := store.Read("session-1")
	if err != nil {
		t.Fatal(err)
	}
	if *read.TotalContextBudgetTokens != 32768 || *read.NativeSessionID != "native-session" || read.SetupScript != record.SetupScript {
		t.Fatalf("record = %+v", read)
	}
	*read.NativeSessionID = "mutated"
	again, err := store.Read("session-1")
	if err != nil || *again.NativeSessionID != "native-session" {
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

func TestConcurrentSessionFieldsSurviveRestart(t *testing.T) {
	root := t.TempDir()
	resources, backing, err := cosistate.Open(root, lifecycle.Namespace, SessionRecordRegistration())
	if err != nil {
		t.Fatal(err)
	}
	store, err := NewStore(filepath.Join(root, "sessions"), resources)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Write("session", Record{Harness: sessionpb.Harness_HARNESS_CLAUDE, CWD: "/workspace", Model: "old-model", ReasoningEffort: "old-effort", EventSourceID: "source"}); err != nil {
		t.Fatal(err)
	}
	start := make(chan struct{})
	var workers sync.WaitGroup
	errors := make(chan error, 3)
	for _, update := range []func() error{
		func() error { return store.UpdateModel("session", "new-model") },
		func() error { return store.UpdateReasoningEffort("session", "new-effort") },
		func() error { return store.UpdateNativeSessionID("session", "native-session") },
	} {
		workers.Add(1)
		go func() {
			defer workers.Done()
			<-start
			errors <- update()
		}()
	}
	close(start)
	workers.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
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
	record, err := restored.Read("session")
	if err != nil {
		t.Fatal(err)
	}
	if record.Model != "new-model" || record.ReasoningEffort != "new-effort" || record.NativeSessionID == nil || *record.NativeSessionID != "native-session" || record.EventSourceID != "source" {
		t.Fatalf("restored concurrent updates = %+v", record)
	}
}

func TestSessionFieldUpdateDoesNotCreateRecord(t *testing.T) {
	resources := state.WrapCore(namespaced.NewState(inmem.Build))
	store, err := NewStore(t.TempDir(), resources)
	if err != nil {
		t.Fatal(err)
	}
	var storageErr *StorageError
	if err := store.UpdateModel("missing", "model"); !errors.As(err, &storageErr) || !state.IsNotFoundError(err) {
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

func TestNewRecordKeepsExactSetupSource(t *testing.T) {
	script := "printf ' setup \\t'\n\necho done"
	record, err := NewRecord(&sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: "/workspace", Model: "route/model"}, script, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !record.MatchesSetupScript(script) || record.MatchesSetupScript("different") || !record.HasSetupScript() {
		t.Fatalf("setup source = %q", record.SetupScript)
	}
}
