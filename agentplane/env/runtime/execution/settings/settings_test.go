package settings

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
)

func TestReadLimits(t *testing.T) {
	got, err := Parse(`{"native":{"max_sessions":3},"codex":{"max_message_bytes":2097152}}`)
	if err != nil {
		t.Fatal(err)
	}
	if got.Native.MaxSessions != 3 || got.Native.MaxProcesses != native.DefaultLimits().MaxProcesses || got.Codex.MaxMessageBytes != 2097152 {
		t.Fatalf("unexpected merged limits: %+v", got)
	}
	for _, value := range []string{`{"native":{"unknown":1}}`, `{"codex":{"max_open_files":0}}`, `[]`, `{} {}`} {
		if _, err := Parse(value); err == nil {
			t.Fatalf("accepted invalid limits %s", value)
		}
	}
}
