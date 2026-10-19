package runner

import (
	"os"
	"path/filepath"
	"testing"
)

func TestIOLimitsRejectsNonBlockTargets(t *testing.T) {
	path := filepath.Join(t.TempDir(), "regular-file")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	cgroups := &AgentCgroups{BlockDevices: []string{path}}
	if _, err := cgroups.ioLimits(); err == nil {
		t.Fatal("ioLimits accepted a regular file")
	}
}
