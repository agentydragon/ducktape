package observability

import (
	"log/slog"
	"testing"
)

func TestRootLogLevel(t *testing.T) {
	for name, test := range map[string]struct {
		levels map[string]string
		want   slog.Level
	}{
		"unset":   {levels: map[string]string{}, want: slog.LevelInfo},
		"debug":   {levels: map[string]string{"root": "DEBUG"}, want: slog.LevelDebug},
		"warning": {levels: map[string]string{"root": "WARNING"}, want: slog.LevelWarn},
		"error":   {levels: map[string]string{"root": "ERROR"}, want: slog.LevelError},
	} {
		t.Run(name, func(t *testing.T) {
			got, err := RootLogLevel(test.levels)
			if err != nil || got != test.want {
				t.Fatalf("RootLogLevel(%v) = %v, %v; want %v", test.levels, got, err, test.want)
			}
		})
	}
}

func TestRootLogLevelRejectsWhatPythonRejectsOrGoCannotHonour(t *testing.T) {
	for name, levels := range map[string]map[string]string{
		"lowercase":    {"root": "debug"},
		"go alias":     {"root": "WARN"},
		"python only":  {"root": "CRITICAL"},
		"empty":        {"root": ""},
		"named logger": {"agentplane.llm_ingress": "DEBUG"},
	} {
		t.Run(name, func(t *testing.T) {
			if level, err := RootLogLevel(levels); err == nil {
				t.Fatalf("RootLogLevel(%v) = %v, want an error", levels, level)
			}
		})
	}
}
