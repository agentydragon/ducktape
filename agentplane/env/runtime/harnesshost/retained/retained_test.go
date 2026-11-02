package retained

import (
	"os"
	"path/filepath"
	"testing"

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
