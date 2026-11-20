package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
)

func TestReadLimits(t *testing.T) {
	path := filepath.Join(t.TempDir(), "limits.json")
	if err := os.WriteFile(path, []byte(`{"native":{"max_sessions":3},"codex":{"max_message_bytes":2097152}}`), 0600); err != nil {
		t.Fatal(err)
	}
	got, err := readLimits(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.Native.MaxSessions != 3 || got.Native.MaxProcesses != native.DefaultLimits().MaxProcesses || got.Codex.MaxMessageBytes != 2097152 {
		t.Fatalf("unexpected merged limits: %+v", got)
	}
	if err := os.WriteFile(path, []byte(`{"native":{"unknown":1}}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = readLimits(path); err == nil {
		t.Fatal("accepted unknown setting")
	}
	if err := os.WriteFile(path, []byte(`{"codex":{"max_open_files":0}}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = readLimits(path); err == nil {
		t.Fatal("accepted zero limit")
	}
	if _, err = readLimits("relative.json"); err == nil {
		t.Fatal("accepted relative path")
	}
}
