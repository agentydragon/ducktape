package runner

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/containerd/cgroups/v3/cgroup2"
	"github.com/google/uuid"
	"github.com/sethvargo/go-retry"
	"go.uber.org/zap"

	"golang.org/x/sys/unix"
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

func cgroupResources(limits ResourceLimits, ioLimits []cgroup2.Entry) *cgroup2.Resources {
	cpuQuota := limits.CPUQuotaMicros
	period := cgroupCPUPeriodMicros
	memoryMax := limits.MemoryMaxBytes
	memorySwapMax := int64(0)
	// A tool OOM need not kill its native parent along with the individual victim.
	// Leader exit still fences all descendants through DestroyProcessGroup.
	oomGroup := false
	pidsMax := limits.PidsMax
	return &cgroup2.Resources{
		CPU:    &cgroup2.CPU{Max: cgroup2.NewCPUMax(&cpuQuota, &period)},
		Memory: &cgroup2.Memory{Max: &memoryMax, Swap: &memorySwapMax, OOMGroup: &oomGroup},
		Pids:   &cgroup2.Pids{Max: pidsMax},
		IO:     &cgroup2.IO{Max: ioLimits},
	}
}

// loadCgroup confines the manager's controller propagation to the delegated parent.
func loadCgroup(path string) (*cgroup2.Manager, error) {
	return cgroup2.Load("/"+filepath.Base(path), cgroup2.WithMountpoint(filepath.Dir(path)))
}

// AgentCgroups describes a delegated cgroup-v2 subtree and the identity used by
// supervised harnesses. BlockDevices must include the state and workspace devices.
type AgentCgroups struct {
	Root            string
	AgentUID        uint32
	AgentGID        uint32
	BlockDevices    []string
	AggregateLimits ResourceLimits
	ProcessLimits   ResourceLimits
}

func (c *AgentCgroups) validateProcessLimits(limits ResourceLimits) error {
	if err := limits.Validate(); err != nil {
		return err
	}
	if limits.MemoryMaxBytes > c.AggregateLimits.MemoryMaxBytes {
		return errors.New("process memory max exceeds aggregate memory max")
	}
	if limits.PidsMax > c.AggregateLimits.PidsMax {
		return errors.New("process PID max exceeds aggregate PID max")
	}
	if limits.CPUQuotaMicros > c.AggregateLimits.CPUQuotaMicros {
		return errors.New("process CPU quota exceeds aggregate CPU quota")
	}
	return nil
}

// Prepare creates the aggregate group, enables the required controllers, installs
// limits, and fences process groups left by a previous runner.
func (c *AgentCgroups) Prepare() error {
	if c == nil || c.Root == "" {
		return errors.New("agent cgroup root is required")
	}
	if err := c.AggregateLimits.Validate(); err != nil {
		return fmt.Errorf("invalid aggregate agent cgroup limits: %w", err)
	}
	if err := c.validateProcessLimits(c.ProcessLimits); err != nil {
		return fmt.Errorf("invalid process agent cgroup limits: %w", err)
	}
	root, err := filepath.Abs(c.Root)
	if err != nil {
		return err
	}
	c.Root = root
	ioLimits, err := c.ioLimits(c.AggregateLimits)
	if err != nil {
		return err
	}
	if len(ioLimits) == 0 {
		return errors.New("agent cgroup requires the state and workspace block devices")
	}
	if _, err := cgroup2.NewManager(filepath.Dir(root), "/"+filepath.Base(root), cgroupResources(c.AggregateLimits, ioLimits)); err != nil {
		return fmt.Errorf("prepare aggregate agent cgroup %s: %w", root, err)
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return fmt.Errorf("list agent cgroup root %s: %w", root, err)
	}
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Name(), "process-") {
			continue
		}
		if !entry.IsDir() {
			return fmt.Errorf("unexpected entry in agent cgroup root: %s", filepath.Join(root, entry.Name()))
		}
		if err := DestroyProcessGroup(filepath.Join(root, entry.Name())); err != nil {
			return err
		}
	}
	return nil
}

// CreateProcessGroup creates one limited cgroup for a native harness and its descendants.
// A non-nil override selects a purpose-specific leaf budget within the aggregate limits.
func (c *AgentCgroups) CreateProcessGroup(override *ResourceLimits) (string, error) {
	if c == nil || c.Root == "" {
		return "", errors.New("agent cgroups are not prepared")
	}
	limits := c.ProcessLimits
	if override != nil {
		limits = *override
	}
	if err := c.validateProcessLimits(limits); err != nil {
		return "", fmt.Errorf("invalid process agent cgroup limits: %w", err)
	}
	ioLimits, err := c.ioLimits(limits)
	if err != nil {
		return "", err
	}
	parent, err := loadCgroup(c.Root)
	if err != nil {
		return "", err
	}
	name := "process-" + uuid.NewString()
	if _, err := parent.NewChild(name, cgroupResources(limits, ioLimits)); err != nil {
		return "", fmt.Errorf("create agent process cgroup: %w", err)
	}
	return filepath.Join(c.Root, name), nil
}

// DestroyProcessGroup kills remaining members and removes the cgroup after it is empty.
func DestroyProcessGroup(path string) error {
	if path == "" {
		return nil
	}
	if _, err := os.Lstat(path); errors.Is(err, os.ErrNotExist) {
		return nil
	} else if err != nil {
		return fmt.Errorf("inspect agent process cgroup %s: %w", path, err)
	}
	manager, err := loadCgroup(path)
	if err != nil {
		return err
	}
	if err := manager.Kill(); err != nil {
		return fmt.Errorf("kill remaining agent cgroup processes in %s: %w", path, err)
	}
	logCgroupMemoryDiagnostics(path)
	backoff := retry.WithMaxDuration(stopGrace, retry.NewConstant(50*time.Millisecond))
	return retry.Do(context.Background(), backoff, func(context.Context) error {
		return retry.RetryableError(manager.Delete())
	})
}

func (c *AgentCgroups) ioLimits(limits ResourceLimits) ([]cgroup2.Entry, error) {
	var entries []cgroup2.Entry
	for _, path := range c.BlockDevices {
		info, err := os.Stat(path)
		if err != nil {
			return nil, fmt.Errorf("inspect agent I/O limit target %s: %w", path, err)
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || info.Mode()&os.ModeDevice == 0 || info.Mode()&os.ModeCharDevice != 0 {
			return nil, fmt.Errorf("agent I/O limit target is not a block device: %s", path)
		}
		for _, limit := range []struct {
			kind cgroup2.IOType
			rate uint64
		}{{cgroup2.ReadBPS, limits.ReadBPS}, {cgroup2.WriteBPS, limits.WriteBPS}, {cgroup2.ReadIOPS, limits.ReadIOPS}, {cgroup2.WriteIOPS, limits.WriteIOPS}} {
			entries = append(entries, cgroup2.Entry{
				Type: limit.kind, Rate: limit.rate,
				Major: int64(unix.Major(uint64(stat.Rdev))), Minor: int64(unix.Minor(uint64(stat.Rdev))),
			})
		}
	}
	return entries, nil
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
