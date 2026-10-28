package runner

import (
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
