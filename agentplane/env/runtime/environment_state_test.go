package runtime

import (
	"os"
	"path/filepath"
	"testing"
)

func TestEnvironmentStateKeepsLegacySealFormatAndPath(t *testing.T) {
	stateDir := t.TempDir()
	state, err := NewEnvironmentState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := state.RequireIncarnationUID(); err == nil {
		t.Fatal("RequireIncarnationUID succeeded before an incarnation was adopted")
	}
	if err := state.AdoptIncarnation("incarnation-one"); err != nil {
		t.Fatal(err)
	}
	if err := state.EnterLameduck(); err != nil {
		t.Fatal(err)
	}

	marker := filepath.Join(stateDir, "sessions", "sealed")
	data, err := os.ReadFile(marker)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "incarnation-one" {
		t.Fatalf("sealed marker payload = %q, want incarnation-one", data)
	}
	if snapshot := state.Snapshot(); snapshot.IncarnationUID != "incarnation-one" || !snapshot.Known || !snapshot.Quiesced {
		t.Fatalf("snapshot after seal = %+v", snapshot)
	}

	if err := state.AdoptIncarnation("incarnation-two"); err != nil {
		t.Fatal(err)
	}
	if state.Lameduck() {
		t.Fatal("new incarnation retained the prior incarnation's sealed marker")
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("legacy marker after adopting a new incarnation: stat err = %v, want not-exist", err)
	}
}

func TestEnvironmentStateReadsLegacyMarkerAcrossRestart(t *testing.T) {
	stateDir := t.TempDir()
	legacyMarker := filepath.Join(stateDir, "sessions", "sealed")
	if err := os.MkdirAll(filepath.Dir(legacyMarker), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(legacyMarker, []byte("legacy-incarnation"), 0o666); err != nil {
		t.Fatal(err)
	}

	state, err := NewEnvironmentState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := state.AdoptIncarnation("legacy-incarnation"); err != nil {
		t.Fatal(err)
	}
	if snapshot := state.Snapshot(); snapshot.IncarnationUID != "legacy-incarnation" || !snapshot.Known || !snapshot.Quiesced {
		t.Fatalf("snapshot for existing legacy marker = %+v", snapshot)
	}
}
