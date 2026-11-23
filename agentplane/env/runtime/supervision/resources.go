package supervision

import (
	"errors"
	"math"
)

const CPUPeriodMicros uint64 = 100000

// WorkloadSlice, ExecutorSlice, and CommandSlice contain native Agentplane services by role.
const (
	WorkloadSlice = "agentplane-workloads.slice"
	ExecutorSlice = "agentplane-executors.slice"
	CommandSlice  = "agentplane-commands.slice"
)

// ResourceLimits is one cgroup-v2 resource budget. CPU quota is expressed in
// microseconds per the fixed CPUPeriodMicros period.
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
	if uint64(r.CPUQuotaMicros) > math.MaxUint64/(1_000_000/CPUPeriodMicros) {
		return errors.New("CPU quota exceeds the systemd property range")
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
