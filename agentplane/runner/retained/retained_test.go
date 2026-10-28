package retained

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
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
	if fields["setup_script"] != nil {
		t.Fatalf("legacy hash-only record unexpectedly gained setup source: %s", data)
	}
}

func TestNewRecordPersistsSetupSourceAndDerivedCompatibilityHash(t *testing.T) {
	script := "printf setup"
	record, err := NewRecord(&runnerpb.SessionSpec{
		Harness: runnerpb.Harness_HARNESS_CLAUDE, Cwd: "/workspace", Model: "route/model",
	}, script, nil)
	if err != nil {
		t.Fatal(err)
	}
	if record.SetupScript != script || record.SetupScriptSHA256 != nil {
		t.Fatalf("new record setup source = %q, legacy hash = %v; want source authority and no stored digest", record.SetupScript, record.SetupScriptSHA256)
	}
	data, err := json.Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	var wireScript, wireHash string
	if err := json.Unmarshal(fields["setup_script"], &wireScript); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(fields["setup_script_sha256"], &wireHash); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte(script))
	if wireScript != script || wireHash != fmt.Sprintf("%x", digest) {
		t.Fatalf("wire setup source=%q hash=%q; want source and derived compatibility hash", wireScript, wireHash)
	}
	var roundTrip Record
	if err := json.Unmarshal(data, &roundTrip); err != nil {
		t.Fatal(err)
	}
	if roundTrip.SetupScript != script || roundTrip.SetupScriptSHA256 != nil || !roundTrip.MatchesSetupScript(script) || roundTrip.MatchesSetupScript("different") {
		t.Fatalf("round trip setup source = %q, legacy hash = %v; source must remain authority", roundTrip.SetupScript, roundTrip.SetupScriptSHA256)
	}
	legacyHash := fmt.Sprintf("%x", digest)
	legacyJSON, err := json.Marshal(sessionRecordJSON{
		Harness: "HARNESS_CLAUDE", CWD: "/workspace", Model: "route/model",
		SetupScriptSHA256: &legacyHash, EventSourceID: "legacy-source",
	})
	if err != nil {
		t.Fatal(err)
	}
	var legacy Record
	if err := json.Unmarshal(legacyJSON, &legacy); err != nil {
		t.Fatal(err)
	}
	if legacy.SetupScript != "" || legacy.SetupScriptSHA256 == nil || !legacy.MatchesSetupScript(script) || legacy.MatchesSetupScript("different") {
		t.Fatalf("legacy hash-only record did not remain readable/matchable: %+v", legacy)
	}
}
