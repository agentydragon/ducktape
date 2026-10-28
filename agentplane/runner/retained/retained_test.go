package retained

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

func TestStorePreservesRunnerSessionJSONShape(t *testing.T) {
	root := t.TempDir()
	store, err := NewStore(root)
	if err != nil {
		t.Fatal(err)
	}
	totalBudget := 32768
	nativeID := "native-session"
	setupHash := "0123456789abcdef"
	record := Record{
		Harness: runnerpb.Harness_HARNESS_CLAUDE, CWD: "/workspace", Model: "route/model",
		TotalContextBudgetTokens: &totalBudget, ReasoningEffort: "medium", Instructions: "Keep going.",
		SetupScriptSHA256: &setupHash, EventSourceID: "bdb9e654-d77c-42a2-b006-9a0dd4f20655", NativeSessionID: &nativeID,
	}
	if err := store.Write("session-1", record); err != nil {
		t.Fatal(err)
	}
	read, err := store.Read("session-1")
	if err != nil {
		t.Fatal(err)
	}
	if read.Harness != record.Harness || read.Spec().GetCwd() != record.CWD || read.EventSourceID != record.EventSourceID || *read.NativeSessionID != nativeID {
		t.Fatalf("session record did not round trip: %+v", read)
	}
	data, err := os.ReadFile(filepath.Join(root, "session-1", "session.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	if string(fields["harness"]) != `"HARNESS_CLAUDE"` || fields["native_session_id"] == nil || fields["event_source_id"] == nil {
		t.Fatalf("persisted session JSON fields = %s", data)
	}
}
