package runner

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"go.uber.org/zap"
)

const cgroupCPUPeriodMicros uint64 = 100000

// ResourceLimits is one cgroup-v2 resource budget. CPU quota is expressed in
// microseconds per the fixed 100,000 microsecond period.
type ResourceLimits struct {
	CPUQuotaMicros int64  `json:"cpu_quota_micros"`
	MemoryMaxBytes int64  `json:"memory_max_bytes"`
	PidsMax        int64  `json:"pids_max"`
	ReadBPS        uint64 `json:"read_bps"`
	WriteBPS       uint64 `json:"write_bps"`
	ReadIOPS       uint64 `json:"read_iops"`
	WriteIOPS      uint64 `json:"write_iops"`
}

// Validate checks values before they are passed to the cgroup-v2 controller.
func (r ResourceLimits) Validate() error {
	if r.CPUQuotaMicros <= 0 {
		return errors.New("CPU quota must be positive")
	}
	if r.MemoryMaxBytes <= 0 {
		return errors.New("memory max must be positive")
	}
	if r.PidsMax <= 0 {
		return errors.New("PID max must be positive")
	}
	if r.ReadBPS == 0 || r.WriteBPS == 0 || r.ReadIOPS == 0 || r.WriteIOPS == 0 {
		return errors.New("read and write I/O limits must be positive")
	}
	return nil
}

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
