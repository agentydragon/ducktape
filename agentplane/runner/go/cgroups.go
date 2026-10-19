package runner

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/containerd/cgroups/v3/cgroup2"
	"github.com/google/uuid"
	"github.com/sethvargo/go-retry"

	"golang.org/x/sys/unix"
)

// TODO: Consider making aggregate and per-process CPU, memory, PID, and I/O limits configurable.
func cgroupResources(cpuQuota, memoryBytes, pids int64, ioLimits []cgroup2.Entry) *cgroup2.Resources {
	return &cgroup2.Resources{
		CPU:    &cgroup2.CPU{Max: cgroup2.NewCPUMax(&cpuQuota, new(uint64(100000)))},
		Memory: &cgroup2.Memory{Max: &memoryBytes, Swap: new(int64(0)), OOMGroup: new(true)},
		Pids:   &cgroup2.Pids{Max: pids},
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
	Root         string
	AgentUID     uint32
	AgentGID     uint32
	BlockDevices []string
}

// Prepare creates the aggregate group, enables the required controllers, installs
// limits, and fences process groups left by a previous runner.
func (c *AgentCgroups) Prepare() error {
	if c == nil || c.Root == "" {
		return errors.New("agent cgroup root is required")
	}
	root, err := filepath.Abs(c.Root)
	if err != nil {
		return err
	}
	c.Root = root
	ioLimits, err := c.ioLimits()
	if err != nil {
		return err
	}
	if len(ioLimits) == 0 {
		return errors.New("agent cgroup requires the state and workspace block devices")
	}
	if _, err := cgroup2.NewManager(filepath.Dir(root), "/"+filepath.Base(root), cgroupResources(400000, 4294967296, 768, ioLimits)); err != nil {
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
func (c *AgentCgroups) CreateProcessGroup() (string, error) {
	if c == nil || c.Root == "" {
		return "", errors.New("agent cgroups are not prepared")
	}
	ioLimits, err := c.ioLimits()
	if err != nil {
		return "", err
	}
	parent, err := loadCgroup(c.Root)
	if err != nil {
		return "", err
	}
	name := "process-" + uuid.NewString()
	if _, err := parent.NewChild(name, cgroupResources(300000, 3221225472, 512, ioLimits)); err != nil {
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
	backoff := retry.WithMaxDuration(stopGrace, retry.NewConstant(50*time.Millisecond))
	return retry.Do(context.Background(), backoff, func(context.Context) error {
		return retry.RetryableError(manager.Delete())
	})
}

func (c *AgentCgroups) ioLimits() ([]cgroup2.Entry, error) {
	var limits []cgroup2.Entry
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
		}{{cgroup2.ReadBPS, 52428800}, {cgroup2.WriteBPS, 20971520}, {cgroup2.ReadIOPS, 1000}, {cgroup2.WriteIOPS, 500}} {
			limits = append(limits, cgroup2.Entry{
				Type: limit.kind, Rate: limit.rate,
				Major: int64(unix.Major(uint64(stat.Rdev))), Minor: int64(unix.Minor(uint64(stat.Rdev))),
			})
		}
	}
	return limits, nil
}
