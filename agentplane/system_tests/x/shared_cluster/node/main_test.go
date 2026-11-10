package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestConfigureLimits(t *testing.T) {
	for _, test := range []struct {
		name, memory, cpu, pids, wantPIDs string
		valid                             bool
	}{
		{"bounded", "1024", "100000 100000", "max", "8192", true},
		{"stricter PID limit", "1024", "100000 100000", "1024", "1024", true},
		{"larger PID limit", "1024", "100000 100000", "16384", "8192", true},
		{"unbounded memory", "max", "100000 100000", "max", "", false},
		{"invalid memory", "1Gi", "100000 100000", "max", "", false},
		{"zero memory", "0", "100000 100000", "max", "", false},
		{"unbounded CPU", "1024", "max 100000", "max", "", false},
		{"missing CPU period", "1024", "100000", "max", "", false},
		{"zero CPU period", "1024", "100000 0", "max", "", false},
		{"invalid PID limit", "1024", "100000 100000", "invalid", "", false},
		{"zero PID limit", "1024", "100000 100000", "0", "", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			root := t.TempDir()
			for name, value := range map[string]string{
				"memory.max": test.memory, "cpu.max": test.cpu, "pids.max": test.pids,
			} {
				if err := os.WriteFile(filepath.Join(root, name), []byte(value+"\n"), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			if err := configureLimits(root); (err == nil) != test.valid {
				t.Fatalf("configureLimits() = %v, want valid %v", err, test.valid)
			}
			if test.valid {
				contents, err := os.ReadFile(filepath.Join(root, "pids.max"))
				if err != nil || strings.TrimSpace(string(contents)) != test.wantPIDs {
					t.Fatalf("pids.max = %q, %v; want %q", contents, err, test.wantPIDs)
				}
			}
		})
	}
}
