package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
)

func TestExecutorRejectsMalformedInputBeforeDispatch(t *testing.T) {
	for _, tc := range []struct {
		name string
		tool string
		args string
	}{
		{"negative exec yield", "exec_command", `{"cmd":"touch /tmp/should-not-run","yield_time_ms":-1}`},
		{"missing cmd", "exec_command", `{"workdir":"/tmp"}`},
		{"non-object exec input", "exec_command", `"touch /tmp/should-not-run"`},
		{"escalation under approval policy never", "exec_command", `{"cmd":"touch /tmp/should-not-run","sandbox_permissions":"require_escalated"}`},
		{"justification without override", "exec_command", `{"cmd":"touch /tmp/should-not-run","justification":"please"}`},
		{"shell the executor does not report", "exec_command", `{"cmd":"touch /tmp/should-not-run","shell":"zsh"}`},
		{"string session_id", "write_stdin", `{"session_id":"test-process"}`},
		{"fractional stdin yield", "write_stdin", `{"session_id":1000,"yield_time_ms":1.5}`},
		{"non-string chars", "write_stdin", `{"session_id":1000,"chars":true}`},
		{"missing session_id", "write_stdin", `{"chars":"x"}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// No store or transport: invalid inputs must fail before accessing either, and are
			// reported to the calling JavaScript rather than failing the Session.
			executor := &Executor{shell: &execShell{Name: "bash", Path: "/bin/bash"}}
			_, err := executor.Invoke(context.Background(), tc.tool, json.RawMessage(tc.args))
			if !errors.As(err, new(*toolError)) {
				t.Fatalf("malformed input error=%v, want a tool error", err)
			}
		})
	}
}

func TestExecutorReadRejectsMissingOutput(t *testing.T) {
	for _, raw := range []string{
		`{}`, `{"chunks":null}`, `{"chunks":[null]}`, `{"chunks":[{}]}`,
		`{"chunks":[{"chunk":null}]}`, `{"chunks":[{"chunk":"invalid base64"}]}`,
	} {
		var result executorReadResult
		if err := json.Unmarshal([]byte(raw), &result); err == nil {
			t.Errorf("accepted malformed process/read result: %s", raw)
		}
	}
	// Pinned exec-server returns an empty array when there is no output yet.
	var result executorReadResult
	if err := json.Unmarshal([]byte(`{"chunks":[],"nextSeq":0,"exited":false,"exitCode":null,"closed":false,"failure":null}`), &result); err != nil {
		t.Fatal(err)
	}
}
