package main

import (
	"strings"
	"testing"
)

func TestObserveInputRequiresItsOwnHistoryConfiguration(t *testing.T) {
	env := map[string]string{
		"AGENTPLANE_SESSIONS_URL":        "http://127.0.0.1:1",
		"AGENTPLANE_SESSIONS_CREDENTIAL": "dispatcher-placeholder",
	}
	code, stdout, stderr := invoke(env, "sessions", "observe-input", "58cae82c-453d-4de7-ae5b-17a8f7596077", "--command-id", "c40dd833-326f-4be1-b42a-4bb74c76c49a")
	if code != exitUsage || stdout != "" || !strings.Contains(stderr, "AGENTPLANE_HISTORY_URL and AGENTPLANE_HISTORY_CREDENTIAL not set") {
		t.Fatalf("exit %d, stdout %q, stderr %q; want independent History configuration error", code, stdout, stderr)
	}
}

func TestObserveInputRejectsWaitOverBoundBeforeConnecting(t *testing.T) {
	code, stdout, stderr := invoke(nil, "sessions", "observe-input", "session", "--command-id", "command", "--wait-seconds", "31")
	if code != exitUsage || stdout != "" || !strings.Contains(stderr, "--wait-seconds must be between 0 and 30") {
		t.Fatalf("exit %d, stdout %q, stderr %q; want bounded wait validation", code, stdout, stderr)
	}
}
