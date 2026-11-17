package configuration

import (
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/prometheus/procfs"
)

type guestResources struct {
	GuestMemoryBytes       int64                       `json:"guest_memory_bytes"`
	GuestVCPUCount         int64                       `json:"guest_vcpu_count"`
	OSReserveBytes         int64                       `json:"os_reserve_bytes"`
	ServiceMemoryHighBytes int64                       `json:"service_memory_high_bytes"`
	ServiceMemoryMaxBytes  int64                       `json:"service_memory_max_bytes"`
	ServiceCPUQuotaMicros  int64                       `json:"service_cpu_quota_micros"`
	ServicePidsMax         int64                       `json:"service_pids_max"`
	Aggregate              supervision.ResourceLimits  `json:"aggregate"`
	Process                supervision.ResourceLimits  `json:"process"`
	Executor               *supervision.ResourceLimits `json:"executor"`
}

func (r guestResources) Validate() error {
	if r.GuestMemoryBytes <= 0 || r.GuestVCPUCount <= 0 || r.OSReserveBytes < 0 || r.OSReserveBytes >= r.GuestMemoryBytes {
		return errors.New("guest memory, vCPU count and OS reserve are inconsistent")
	}
	if r.GuestVCPUCount > int64(^uint64(0)>>1)/int64(supervision.CPUPeriodMicros) {
		return errors.New("guest vCPU count is too large")
	}
	if r.ServiceMemoryHighBytes <= 0 || r.ServiceMemoryMaxBytes < r.ServiceMemoryHighBytes || r.ServiceMemoryMaxBytes > r.GuestMemoryBytes-r.OSReserveBytes {
		return errors.New("service memory caps must leave the configured OS reserve")
	}
	if r.ServiceCPUQuotaMicros <= 0 || r.ServiceCPUQuotaMicros > r.GuestVCPUCount*int64(supervision.CPUPeriodMicros) || r.ServiceCPUQuotaMicros%1000 != 0 {
		return errors.New("service CPU quota must fit guest vCPU capacity and map to an integral systemd CPUQuota percent")
	}
	if r.ServicePidsMax <= 0 {
		return errors.New("service PID max must be positive")
	}
	groups := supervision.SystemdLauncher{AggregateLimits: r.Aggregate, ProcessLimits: r.Process}
	if err := groups.AggregateLimits.Validate(); err != nil {
		return fmt.Errorf("invalid aggregate limits: %w", err)
	}
	if err := groups.ValidateProcessLimits(groups.ProcessLimits); err != nil {
		return fmt.Errorf("invalid process limits: %w", err)
	}
	if r.Aggregate.MemoryMaxBytes > r.ServiceMemoryHighBytes || r.Aggregate.PidsMax > r.ServicePidsMax || r.Aggregate.CPUQuotaMicros > r.ServiceCPUQuotaMicros {
		return errors.New("aggregate limits must fit within Environment Runtime service limits")
	}
	if r.Executor != nil {
		if err := r.Executor.Validate(); err != nil {
			return fmt.Errorf("invalid executor limits: %w", err)
		}
		// Subtraction keeps the combined-cap checks safe from integer overflow.
		if r.Executor.MemoryMaxBytes > r.ServiceMemoryHighBytes-r.Aggregate.MemoryMaxBytes {
			return errors.New("harness and executor memory caps must fit below service memory high")
		}
		if r.Executor.PidsMax >= r.ServicePidsMax-r.Aggregate.PidsMax {
			return errors.New("harness and executor PID caps must leave room for Environment Runtime and supervisors")
		}
		if r.Executor.CPUQuotaMicros > r.ServiceCPUQuotaMicros {
			return errors.New("executor CPU quota must fit within Environment Runtime service quota")
		}
	}
	return nil
}

// guestResourceSystemdProperties reads and validates the shared configuration before returning
// systemd properties. The VM applies these before starting the Environment Runtime, so this
// path must reject malformed limits before systemctl is invoked.
func GuestResourceSystemdProperties(configPath string) ([]string, error) {
	return guestResourceSystemdPropertiesWithProcRoot(configPath, "/proc")
}

func guestResourceSystemdPropertiesWithProcRoot(configPath, procRoot string) ([]string, error) {
	file, err := readConfiguration(configPath)
	if err != nil {
		return nil, err
	}
	_, _, vm, _, _, err := file.resolveEnvironment()
	if err != nil {
		return nil, fmt.Errorf("invalid VM environment: %w", err)
	}
	if vm == nil {
		return nil, errors.New("resource preflight requires a VM environment")
	}
	actualMemory, err := guestMemoryTotalBytes(procRoot)
	if err != nil {
		return nil, fmt.Errorf("cannot verify guest memory for config at %s: %w", configPath, err)
	}
	if err := validateGuestMemoryCapacity(vm.Resources, actualMemory); err != nil {
		return nil, fmt.Errorf("invalid Environment Runtime config at %s: %w", configPath, err)
	}
	return guestResourceProperties(vm.Resources), nil
}

func guestResourceProperties(resources guestResources) []string {
	return []string{
		fmt.Sprintf("MemoryHigh=%d", resources.ServiceMemoryHighBytes),
		fmt.Sprintf("MemoryMax=%d", resources.ServiceMemoryMaxBytes),
		"MemorySwapMax=0",
		fmt.Sprintf("CPUQuota=%d%%", resources.ServiceCPUQuotaMicros/1000),
		fmt.Sprintf("TasksMax=%d", resources.ServicePidsMax),
	}
}

func validateGuestMemoryCapacity(resources guestResources, actualMemoryBytes int64) error {
	if actualMemoryBytes <= 0 {
		return errors.New("guest reports no available memory")
	}
	if resources.ServiceMemoryMaxBytes >= actualMemoryBytes {
		return fmt.Errorf("service memory max %d bytes must be below guest MemTotal %d bytes", resources.ServiceMemoryMaxBytes, actualMemoryBytes)
	}
	return nil
}

func guestMemoryTotalBytes(procRoot string) (int64, error) {
	filesystem, err := procfs.NewFS(procRoot)
	if err != nil {
		return 0, err
	}
	meminfo, err := filesystem.Meminfo()
	if err != nil {
		return 0, err
	}
	if meminfo.MemTotalBytes == nil {
		return 0, fmt.Errorf("MemTotal is missing from proc filesystem %s", procRoot)
	}
	if *meminfo.MemTotalBytes == 0 {
		return 0, fmt.Errorf("MemTotal is zero in proc filesystem %s", procRoot)
	}
	if *meminfo.MemTotalBytes > uint64(^uint64(0)>>1) {
		return 0, fmt.Errorf("MemTotal is too large in proc filesystem %s", procRoot)
	}
	return int64(*meminfo.MemTotalBytes), nil
}
