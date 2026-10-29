package centralsampler

import (
	"bytes"
	"encoding/json"
	"slices"
	"strings"
	"testing"
)

var testBash = &execShell{Name: "bash", Path: "/bin/bash"}

func TestPlanExecCommandClampsYield(t *testing.T) {
	for _, tc := range []struct {
		args string
		want int
	}{
		{`{"cmd":"true"}`, 10_000},
		{`{"cmd":"true","yield_time_ms":0}`, 250},
		{`{"cmd":"true","yield_time_ms":99999}`, 30_000},
		{`{"cmd":"true","yield_time_ms":1234}`, 1234},
	} {
		plan, err := planExecCommand(json.RawMessage(tc.args), "/work", testBash)
		if err != nil {
			t.Fatal(err)
		}
		if plan.yieldMS != tc.want {
			t.Errorf("%s yield=%d, want %d", tc.args, plan.yieldMS, tc.want)
		}
	}
}

func TestPlanExecCommandResolvesShellAndWorkdir(t *testing.T) {
	for _, tc := range []struct {
		name     string
		args     string
		shell    *execShell
		wantArgv []string
		wantCwd  string
	}{
		{"reported shell runs as login shell by default", `{"cmd":"echo hi"}`, testBash, []string{"/bin/bash", "-lc", "echo hi"}, "/work"},
		{"login false", `{"cmd":"echo hi","login":false}`, testBash, []string{"/bin/bash", "-c", "echo hi"}, "/work"},
		{"matching requested shell launches the reported path", `{"cmd":"echo hi","shell":"/usr/local/bin/bash"}`, testBash, []string{"/bin/bash", "-lc", "echo hi"}, "/work"},
		{"relative workdir joins the session cwd", `{"cmd":"pwd","workdir":"sub/dir"}`, testBash, []string{"/bin/bash", "-lc", "pwd"}, "/work/sub/dir"},
		{"absolute workdir", `{"cmd":"pwd","workdir":"/srv"}`, testBash, []string{"/bin/bash", "-lc", "pwd"}, "/srv"},
		{"executor without a reported shell", `{"cmd":"echo hi"}`, nil, []string{"/bin/sh", "-lc", "echo hi"}, "/work"},
		{"unknown fields ignored and null means omitted", `{"cmd":"echo hi","tty":null,"future_field":1}`, testBash, []string{"/bin/bash", "-lc", "echo hi"}, "/work"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			plan, err := planExecCommand(json.RawMessage(tc.args), "/work", tc.shell)
			if err != nil {
				t.Fatal(err)
			}
			if !slices.Equal(plan.argv, tc.wantArgv) || plan.cwd != tc.wantCwd {
				t.Fatalf("argv=%q cwd=%q, want %q %q", plan.argv, plan.cwd, tc.wantArgv, tc.wantCwd)
			}
		})
	}
}

func TestPlanWriteStdinYieldBounds(t *testing.T) {
	for _, tc := range []struct {
		args string
		want int
	}{
		{`{"session_id":1000,"chars":"x"}`, 250},
		{`{"session_id":1000,"chars":"x","yield_time_ms":99999}`, 30_000},
		{`{"session_id":1000}`, 5_000},
		{`{"session_id":1000,"yield_time_ms":60000}`, 60_000},
		{`{"session_id":1000,"yield_time_ms":999999}`, 300_000},
	} {
		plan, err := planWriteStdin(json.RawMessage(tc.args))
		if err != nil {
			t.Fatal(err)
		}
		if plan.yieldMS != tc.want {
			t.Errorf("%s yield=%d, want %d", tc.args, plan.yieldMS, tc.want)
		}
	}
}

func TestHeadTailBufferKeepsBothEndsAndRestores(t *testing.T) {
	buffer := newHeadTailBuffer(8)
	buffer.push([]byte("abc"))
	buffer.push([]byte("defghij"))
	buffer.push([]byte("klm"))
	if got := string(buffer.retained()); got != "abcdjklm" || buffer.omitted != 5 || buffer.totalBytes() != 13 {
		t.Fatalf("retained=%q omitted=%d total=%d", got, buffer.omitted, buffer.totalBytes())
	}
	if got := string(buffer.withOmissionMarker()); got != "abcd\n... 5 bytes omitted ...\njklm" {
		t.Fatalf("model view=%q", got)
	}
	restored, err := restoreHeadTailBuffer(8, buffer.retained(), buffer.omitted)
	if err != nil {
		t.Fatal(err)
	}
	restored.push([]byte("n"))
	buffer.push([]byte("n"))
	if !bytes.Equal(restored.retained(), buffer.retained()) || restored.omitted != buffer.omitted {
		t.Fatalf("restored buffer diverged: %q/%d vs %q/%d", restored.retained(), restored.omitted, buffer.retained(), buffer.omitted)
	}
	if _, err := restoreHeadTailBuffer(8, []byte("short"), 3); err == nil {
		t.Fatal("accepted an omission count with partial halves")
	}
}

func TestUnifiedExecResultShape(t *testing.T) {
	output := newHeadTailBuffer(unifiedExecOutputMaxBytes)
	output.push([]byte("hello\n"))
	sessionID, budget := 4242, 1
	running := processObservation{output: output, wallTime: 0.5}.unifiedExecResult(&sessionID, nil, "abc123")
	if running.SessionID == nil || *running.SessionID != 4242 || running.ExitCode != nil || running.Output != "hello\n" ||
		running.OriginalTokenCount == nil || *running.OriginalTokenCount != 2 {
		t.Fatalf("running result=%+v", running)
	}
	code := 0
	exited := processObservation{output: output, exitCode: &code, exited: true}.unifiedExecResult(&sessionID, &budget, "abc123")
	if exited.SessionID != nil || exited.ExitCode == nil || *exited.ExitCode != 0 {
		t.Fatalf("exited result=%+v", exited)
	}
	if !strings.HasPrefix(exited.Output, "Warning: truncated output (original token count: 2)\nTotal output lines: 1\n\n") ||
		!strings.Contains(exited.Output, "tokens truncated") {
		t.Fatalf("budgeted output=%q", exited.Output)
	}
	encoded, err := json.Marshal(exited)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(encoded, &fields); err != nil {
		t.Fatal(err)
	}
	if _, present := fields["session_id"]; present {
		t.Fatalf("exited result offers a session: %s", encoded)
	}
}

func TestTruncateExecOutputKeepsOmissionNotice(t *testing.T) {
	output := newHeadTailBuffer(16)
	output.push(bytes.Repeat([]byte("x"), 40))
	text := truncateExecOutput(output.withOmissionMarker(), output.omitted, 10, 2)
	if !strings.HasPrefix(text, "Warning: truncated output (original token count: 10)\n... 24 bytes omitted ...\n\n") {
		t.Fatalf("truncated=%q", text)
	}
	if fits := truncateExecOutput(output.withOmissionMarker(), output.omitted, 10, 1000); fits != string(output.withOmissionMarker()) {
		t.Fatalf("output within budget changed: %q", fits)
	}
}
