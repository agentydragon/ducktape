package retained

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func TestStorePreservesRunnerSessionJSONShape(t *testing.T) {
	root := t.TempDir()
	store, err := NewStore(root)
	if err != nil {
		t.Fatal(err)
	}
	totalBudget := 32768
	nativeID := "native-session"
	record := Record{
		Harness: sessionpb.Harness_HARNESS_CLAUDE, CWD: "/workspace", Model: "route/model",
		TotalContextBudgetTokens: &totalBudget, ReasoningEffort: "medium", Instructions: "Keep going.",
		EventSourceID: "bdb9e654-d77c-42a2-b006-9a0dd4f20655", NativeSessionID: &nativeID,
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
	if fields["setup_script"] != nil || fields["setup_script_sha256"] != nil {
		t.Fatalf("record without setup source contains setup fields: %s", data)
	}
}

func TestNewRecordPersistsExactSetupSource(t *testing.T) {
	script := "printf ' setup \\t'\n\necho done"
	record, err := NewRecord(&sessionpb.SessionSpec{
		Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: "/workspace", Model: "route/model",
	}, script, nil)
	if err != nil {
		t.Fatal(err)
	}
	if record.SetupScript != script {
		t.Fatalf("new record setup source = %q; want exact source %q", record.SetupScript, script)
	}
	data, err := json.Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	var wireScript string
	if err := json.Unmarshal(fields["setup_script"], &wireScript); err != nil {
		t.Fatal(err)
	}
	if wireScript != script || fields["setup_script_sha256"] != nil {
		t.Fatalf("wire setup source=%q setup digest=%s; want exact source and no digest", wireScript, fields["setup_script_sha256"])
	}
	var roundTrip Record
	if err := json.Unmarshal(data, &roundTrip); err != nil {
		t.Fatal(err)
	}
	if roundTrip.SetupScript != script || !roundTrip.MatchesSetupScript(script) || roundTrip.MatchesSetupScript("different") {
		t.Fatalf("round trip setup source = %q; want exact source to remain authority", roundTrip.SetupScript)
	}
	if !roundTrip.HasSetupScript() {
		t.Fatal("round-tripped setup source was not retained")
	}
}

func TestUnmarshalRejectsUnknownSetupDigestField(t *testing.T) {
	data := []byte(`{"harness":"HARNESS_CLAUDE","cwd":"/workspace","model":"route/model","total_context_budget_tokens":null,"reasoning_effort":"","instructions":"","setup_script_sha256":"0123456789abcdef","event_source_id":"legacy-source","native_session_id":null}`)
	var record Record
	err := json.Unmarshal(data, &record)
	if err == nil || !strings.Contains(err.Error(), `unknown field "setup_script_sha256"`) {
		t.Fatalf("record with removed setup digest field error = %v, want unknown-field error", err)
	}
}
