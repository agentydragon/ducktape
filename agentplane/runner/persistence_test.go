package runner

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"google.golang.org/protobuf/encoding/protojson"
)

func TestInitializationLogTruncatesOnlyIncompleteTail(t *testing.T) {
	path := filepath.Join(t.TempDir(), "initialization.jsonl")
	first := &runnerpb.InitializationEvent{
		Sequence: 1, Attempt: 1,
		Observation: &runnerpb.InitializationEvent_Output{Output: &runnerpb.InitializationOutput{
			Stream: runnerpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, Data: []byte("ready"),
		}},
	}
	line, err := (protojson.MarshalOptions{UseProtoNames: true}).Marshal(first)
	if err != nil {
		t.Fatal(err)
	}
	valid := append(append([]byte(nil), line...), '\n')
	data := append(append([]byte(nil), valid...), []byte(`{"sequence":2,"attempt":1,"output":{"stream":"unterminated`)...)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	log, err := OpenInitializationLog(path)
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	if info, err := os.Stat(path); err != nil || info.Size() != int64(len(valid)) {
		t.Fatalf("truncated initialization size = %v, %v", info, err)
	}
	result, err := log.AppendResult(1, 0)
	if err != nil || !result.GetExecuted() {
		t.Fatalf("append result = %v, %v", result, err)
	}
	events := log.Since(0)
	if len(events) != 2 || events[0].GetOutput().GetStream() != runnerpb.InitializationStream_INITIALIZATION_STREAM_STDOUT || events[1].GetSequence() != 2 || !log.Ended() {
		t.Fatalf("initialization events after recovery = %v", events)
	}
}

func TestSessionStorePreservesRunnerSessionJSONShape(t *testing.T) {
	root := t.TempDir()
	store, err := NewSessionStore(root)
	if err != nil {
		t.Fatal(err)
	}
	totalBudget := 32768
	nativeID := "native-session"
	setupHash := "0123456789abcdef"
	record := SessionRecord{
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
