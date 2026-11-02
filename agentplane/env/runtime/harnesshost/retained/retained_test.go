package retained

import (
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
	resources, backing, err := cosistate.Open(root, lifecycle.Namespace, SessionRecordCodec())
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
	reopened, reopenedBacking, err := cosistate.Open(root, lifecycle.Namespace, SessionRecordCodec())
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
