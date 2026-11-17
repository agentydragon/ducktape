package systemd

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"go.uber.org/zap"
)

func logCgroupMemoryDiagnostics(path string) {
	fields := []zap.Field{zap.String("cgroup", path)}
	contents, err := os.ReadFile(filepath.Join(path, "memory.events"))
	if err != nil {
		fields = append(fields, zap.String("memory_events_error", err.Error()))
	} else {
		events := map[string]uint64{}
		for _, line := range strings.Split(strings.TrimSpace(string(contents)), "\n") {
			parts := strings.Fields(line)
			if len(parts) != 2 {
				continue
			}
			value, parseErr := strconv.ParseUint(parts[1], 10, 64)
			if parseErr == nil {
				events[parts[0]] = value
			}
		}
		fields = append(fields, zap.Any("memory_events", events))
	}
	peak, err := os.ReadFile(filepath.Join(path, "memory.peak"))
	if err != nil {
		fields = append(fields, zap.String("memory_peak_error", err.Error()))
	} else if value, parseErr := strconv.ParseUint(strings.TrimSpace(string(peak)), 10, 64); parseErr != nil {
		fields = append(fields, zap.String("memory_peak_error", parseErr.Error()))
	} else {
		fields = append(fields, zap.Uint64("memory_peak_bytes", value))
	}
	zap.L().Info("agent process cgroup memory diagnostics", fields...)
}
