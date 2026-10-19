package runner

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

var cgroupControllers = []string{"cpu", "io", "memory", "pids"}

// TODO: Consider making aggregate and per-process CPU, memory, PID, and I/O limits configurable.
const ioLimit = "rbps=52428800 wbps=20971520 riops=1000 wiops=500"

var aggregateCgroupLimits = map[string]string{
	"cpu.max":          "400000 100000",
	"memory.max":       "4294967296",
	"memory.oom.group": "1",
	"memory.swap.max":  "0",
	"pids.max":         "768",
}

var processCgroupLimits = map[string]string{
	"cpu.max":          "300000 100000",
	"memory.max":       "3221225472",
	"memory.oom.group": "1",
	"memory.swap.max":  "0",
	"pids.max":         "512",
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
	parent := filepath.Dir(root)
	if err := enableCgroupControllers(parent); err != nil {
		return err
	}
	if err := os.MkdirAll(root, 0o750); err != nil {
		return fmt.Errorf("create agent cgroup root %s: %w", root, err)
	}
	if err := enableCgroupControllers(root); err != nil {
		return err
	}
	devices, err := c.blockDeviceIDs()
	if err != nil {
		return err
	}
	if len(devices) == 0 {
		return errors.New("agent cgroup requires the state and workspace block devices")
	}
	if err := writeCgroupLimits(root, aggregateCgroupLimits, devices); err != nil {
		return err
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
	devices, err := c.blockDeviceIDs()
	if err != nil {
		return "", err
	}
	var random [16]byte
	if _, err := rand.Read(random[:]); err != nil {
		return "", fmt.Errorf("generate agent process cgroup ID: %w", err)
	}
	path := filepath.Join(c.Root, "process-"+hex.EncodeToString(random[:]))
	if err := os.Mkdir(path, 0o750); err != nil {
		return "", fmt.Errorf("create agent process cgroup %s: %w", path, err)
	}
	if err := writeCgroupLimits(path, processCgroupLimits, devices); err != nil {
		_ = os.Remove(path)
		return "", err
	}
	return path, nil
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
	if err := writeCgroupControl(filepath.Join(path, "cgroup.kill"), "1\n"); err != nil {
		return fmt.Errorf("kill remaining agent cgroup processes in %s: %w", path, err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		empty, err := cgroupIsEmpty(filepath.Join(path, "cgroup.events"))
		if err != nil {
			return err
		}
		if empty {
			if err := os.Remove(path); err != nil {
				return fmt.Errorf("remove agent process cgroup %s: %w", path, err)
			}
			return nil
		}
		time.Sleep(50 * time.Millisecond)
	}
	return fmt.Errorf("agent cgroup still has processes after cgroup.kill: %s", path)
}

func enableCgroupControllers(path string) error {
	data, err := os.ReadFile(filepath.Join(path, "cgroup.controllers"))
	if err != nil {
		return fmt.Errorf("read cgroup controllers at %s: %w", path, err)
	}
	available := make(map[string]bool)
	for _, name := range strings.Fields(string(data)) {
		available[name] = true
	}
	var requested []string
	for _, name := range cgroupControllers {
		if !available[name] {
			return fmt.Errorf("agent cgroup controller %s unavailable at %s", name, path)
		}
		requested = append(requested, "+"+name)
	}
	enabled, err := os.ReadFile(filepath.Join(path, "cgroup.subtree_control"))
	if err != nil {
		return fmt.Errorf("read cgroup subtree controls at %s: %w", path, err)
	}
	set := make(map[string]bool)
	for _, name := range strings.Fields(string(enabled)) {
		set[strings.TrimPrefix(name, "+")] = true
	}
	missing := requested[:0]
	for _, name := range cgroupControllers {
		if !set[name] {
			missing = append(missing, "+"+name)
		}
	}
	if len(missing) == 0 {
		return nil
	}
	if err := writeCgroupControl(filepath.Join(path, "cgroup.subtree_control"), strings.Join(missing, " ")+"\n"); err != nil {
		return fmt.Errorf("enable cgroup controllers at %s: %w", path, err)
	}
	return nil
}

func (c *AgentCgroups) blockDeviceIDs() ([]string, error) {
	devices := make([]string, 0, len(c.BlockDevices))
	for _, path := range c.BlockDevices {
		info, err := os.Stat(path)
		if err != nil {
			return nil, fmt.Errorf("inspect agent I/O limit target %s: %w", path, err)
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || info.Mode()&os.ModeDevice == 0 || info.Mode()&os.ModeCharDevice != 0 {
			return nil, fmt.Errorf("agent I/O limit target is not a block device: %s", path)
		}
		devices = append(devices, fmt.Sprintf("%d:%d", unix.Major(uint64(stat.Rdev)), unix.Minor(uint64(stat.Rdev))))
	}
	return devices, nil
}

func writeCgroupLimits(path string, limits map[string]string, devices []string) error {
	for name, value := range limits {
		control := filepath.Join(path, name)
		if _, err := os.Stat(control); err != nil {
			return fmt.Errorf("required agent cgroup control is absent: %s", control)
		}
		if err := writeCgroupControl(control, value+"\n"); err != nil {
			return fmt.Errorf("write agent cgroup control %s: %w", control, err)
		}
	}
	control := filepath.Join(path, "io.max")
	if _, err := os.Stat(control); err != nil {
		return fmt.Errorf("required agent cgroup control is absent: %s", control)
	}
	for _, device := range devices {
		if err := writeCgroupControl(control, device+" "+ioLimit+"\n"); err != nil {
			return fmt.Errorf("write agent I/O limit for %s: %w", device, err)
		}
	}
	return nil
}

func writeCgroupControl(path, value string) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_TRUNC, 0)
	if err != nil {
		return err
	}
	_, writeErr := file.WriteString(value)
	closeErr := file.Close()
	return errors.Join(writeErr, closeErr)
}

func cgroupIsEmpty(path string) (bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return false, fmt.Errorf("read cgroup events %s: %w", path, err)
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(line) == "populated 0" {
			return true, nil
		}
	}
	return false, nil
}
