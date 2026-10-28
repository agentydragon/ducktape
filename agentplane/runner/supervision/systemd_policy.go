package supervision

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
)

const (
	runnerUnit              = "agentplane-runner.service"
	runnerLifetimeDirectory = "/run/agentplane-runner-lifetime"
)

// SystemdPolicy describes systemd-owned containment, independently of a launch.
// Prepare assigns scope from durable owner identity before any Session store opens.
type SystemdPolicy struct {
	Slice                          string
	AgentUID, AgentGID             uint32
	BlockDevices                   []string
	AggregateLimits, ProcessLimits ResourceLimits
	scope                          string
	lifetimeUnit                   string
	lifetimeMarker                 string
	startTransient                 func(context.Context, *systemd.Conn, string, []systemd.Property, chan<- string) (int, error)
}

// ValidateProcessLimits checks values and ensures one process remains within its aggregate budget.
func (p *SystemdPolicy) ValidateProcessLimits(limits ResourceLimits) error {
	if err := limits.Validate(); err != nil {
		return err
	}
	if limits.MemoryMaxBytes > p.AggregateLimits.MemoryMaxBytes || limits.PidsMax > p.AggregateLimits.PidsMax || limits.CPUQuotaMicros > p.AggregateLimits.CPUQuotaMicros {
		return errors.New("process resource limits exceed aggregate limits")
	}
	return nil
}

type systemdExec struct {
	Path      string
	Arguments []string
	Flags     []string
}

type systemdCondition struct {
	Type            string
	Trigger, Negate bool
	Parameter       string
}

type systemdDeviceLimit struct {
	Path  string
	Limit uint64
}

func property(name string, value any) systemd.Property {
	return systemd.Property{Name: name, Value: dbus.MakeVariant(value)}
}

func (p *SystemdPolicy) properties(limits ResourceLimits) ([]systemd.Property, error) {
	if err := p.ValidateProcessLimits(limits); err != nil {
		return nil, err
	}
	props := []systemd.Property{
		property("CPUQuotaPerSecUSec", uint64(limits.CPUQuotaMicros)*10), property("CPUQuotaPeriodUSec", CPUPeriodMicros),
		property("MemoryMax", uint64(limits.MemoryMaxBytes)), property("MemorySwapMax", uint64(0)), property("TasksMax", uint64(limits.PidsMax)),
	}
	for name, rate := range map[string]uint64{"IOReadBandwidthMax": limits.ReadBPS, "IOWriteBandwidthMax": limits.WriteBPS, "IOReadIOPSMax": limits.ReadIOPS, "IOWriteIOPSMax": limits.WriteIOPS} {
		entries := []systemdDeviceLimit{}
		for _, device := range p.BlockDevices {
			info, err := os.Stat(device)
			if err != nil {
				return nil, err
			}
			if info.Mode()&os.ModeDevice == 0 || info.Mode()&os.ModeCharDevice != 0 {
				return nil, fmt.Errorf("I/O limit target is not a block device: %s", device)
			}
			entries = append(entries, systemdDeviceLimit{device, rate})
		}
		if len(entries) == 0 {
			return nil, errors.New("VM supervision requires block device limits")
		}
		props = append(props, property(name, entries))
	}
	return props, nil
}

// The connection owns its lifetime independently of any Process.Start caller.
// Bound authentication/setup, then retain the connection until fencing completes.
func openSystemdConnection() (*systemd.Conn, context.CancelFunc, error) {
	lifetime, cancel := context.WithCancel(context.Background())
	timer := time.AfterFunc(30*time.Second, cancel)
	conn, err := systemd.NewSystemdConnectionContext(lifetime)
	if !timer.Stop() {
		cancel()
	}
	if err == nil {
		err = lifetime.Err()
	}
	if err != nil {
		if conn != nil {
			conn.Close()
		}
		cancel()
		return nil, nil, err
	}
	return conn, cancel, nil
}

func systemdMissing(err error) bool {
	var e dbus.Error
	return errors.As(err, &e) && (e.Name == "org.freedesktop.systemd1.NoSuchUnit" || e.Name == "org.freedesktop.systemd1.LoadFailed" || e.Name == "org.freedesktop.DBus.Error.UnknownObject")
}

// Prepare validates and applies the owner scope, fences units left by a prior
// Runner incarnation, and returns a factory for processes in that scope.
func (p *SystemdPolicy) Prepare(stateDir, sandboxUID string) (*Supervisor, error) {
	if p == nil {
		return nil, errors.New("VM process policy is required")
	}
	if sandboxUID == "" {
		return nil, errors.New("VM supervision requires a Sandbox owner UID")
	}
	if p.Slice != WorkloadSlice && p.Slice != ExecutorSlice {
		return nil, errors.New("invalid native systemd slice")
	}
	if p.AgentUID == 0 || p.AgentGID == 0 {
		return nil, errors.New("VM native services require a non-root identity")
	}
	if err := p.AggregateLimits.Validate(); err != nil {
		return nil, err
	}
	if err := p.ValidateProcessLimits(p.ProcessLimits); err != nil {
		return nil, err
	}
	marker, err := prepareRunnerLifetime()
	if err != nil {
		return nil, err
	}
	p.lifetimeMarker = marker
	sum := sha256.Sum256([]byte(stateDir + "\x00" + sandboxUID))
	p.scope = fmt.Sprintf("agentplane-native-%x-", sum[:8])
	conn, cancelConnection, err := openSystemdConnection()
	if err != nil {
		return nil, err
	}
	defer cancelConnection()
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	props, err := p.properties(p.AggregateLimits)
	if err != nil {
		return nil, err
	}
	if err := conn.SetUnitPropertiesContext(ctx, p.Slice, true, props...); err != nil {
		return nil, err
	}
	units, err := conn.ListUnitsByPatternsContext(ctx, []string{}, []string{p.scope + "*.service"})
	if err != nil {
		return nil, err
	}
	for _, unit := range units {
		service, err := conn.GetUnitTypePropertiesContext(ctx, unit.Name, "Service")
		if err != nil {
			return nil, err
		}
		if service["Slice"] != p.Slice {
			continue
		}
		if err := fenceSystemdUnit(ctx, conn, unit.Name, true, ""); err != nil {
			return nil, fmt.Errorf("fence prior native unit %s: %w", unit.Name, err)
		}
		_ = conn.ResetFailedUnitContext(ctx, unit.Name)
	}
	return p.newSupervisor(), nil
}

// newSupervisor creates a factory from an immutable snapshot of prepared policy state.
func (p *SystemdPolicy) newSupervisor() *Supervisor {
	prepared := *p
	prepared.BlockDevices = append([]string(nil), p.BlockDevices...)
	return &Supervisor{makeBackend: func(config ProcessConfig) processBackend {
		policy := prepared
		policy.BlockDevices = append([]string(nil), prepared.BlockDevices...)
		return &systemdBackend{config: config, policy: &policy}
	}}
}

// systemd removes this directory synchronously when the Runner service dies.
// An old queued launch therefore cannot cross exec after a successor starts.
func prepareRunnerLifetime() (string, error) {
	invocation := os.Getenv("INVOCATION_ID")
	if len(invocation) != 32 {
		return "", errors.New("VM supervision requires a systemd INVOCATION_ID")
	}
	if _, err := hex.DecodeString(invocation); err != nil {
		return "", fmt.Errorf("invalid systemd INVOCATION_ID: %w", err)
	}
	directory, err := os.Lstat(runnerLifetimeDirectory)
	if err != nil {
		return "", fmt.Errorf("inspect systemd Runner lifetime directory: %w", err)
	}
	owner, ok := directory.Sys().(*syscall.Stat_t)
	if !directory.IsDir() || !ok || owner.Uid != 0 || directory.Mode().Perm()&0022 != 0 {
		return "", errors.New("systemd Runner lifetime directory must be owned and writable only by root")
	}
	marker := filepath.Join(runnerLifetimeDirectory, invocation)
	file, err := os.OpenFile(marker, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0444)
	if err == nil {
		err = errors.Join(file.Chmod(0444), file.Close())
		if err != nil {
			return "", fmt.Errorf("create Runner lifetime marker: %w", err)
		}
	} else if !errors.Is(err, os.ErrExist) {
		return "", fmt.Errorf("create Runner lifetime marker: %w", err)
	}
	info, err := os.Lstat(marker)
	if err != nil {
		return "", err
	}
	owner, ok = info.Sys().(*syscall.Stat_t)
	if !info.Mode().IsRegular() || !ok || owner.Uid != 0 || info.Mode().Perm() != 0444 {
		return "", errors.New("Runner lifetime marker must be a root-owned read-only regular file")
	}
	return marker, nil
}

// fenceSystemdUnit only returns after the entire unit execution domain is empty.
// Stop completion and a vanished main PID are insufficient evidence on their own.
func fenceSystemdUnit(ctx context.Context, conn *systemd.Conn, name string, ordered bool, group string) error {
	// Capture the kernel domain before StopUnit can collect an inactive unit.
	service, err := conn.GetUnitTypePropertiesContext(ctx, name, "Service")
	if err != nil && !systemdMissing(err) {
		return err
	}
	if current, _ := service["ControlGroup"].(string); current != "" {
		group = current
	}
	unit, unitErr := conn.GetUnitPropertiesContext(ctx, name)
	if unitErr != nil && !systemdMissing(unitErr) {
		return unitErr
	}
	if unit["LoadState"] == "loaded" {
		ordered = true
	}
	if !ordered {
		return errors.New("native launch acceptance remains unknown after transport loss")
	}
	if _, err := conn.StopUnitContext(ctx, name, "replace", nil); err != nil && !systemdMissing(err) {
		return err
	}
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		props, err := conn.GetUnitPropertiesContext(ctx, name)
		if err != nil && !systemdMissing(err) {
			return err
		}
		state, _ := props["ActiveState"].(string)
		if systemdMissing(err) || state == "inactive" || state == "failed" {
			empty, err := systemdGroupEmpty(group)
			if err != nil {
				return err
			}
			if empty {
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

// cgroup.events populated accounts for the entire subtree, including descendants
// that escaped their leader's process group. A removed cgroup is also empty.
func systemdGroupEmpty(group string) (bool, error) {
	if group == "" {
		return true, nil
	}
	data, err := os.ReadFile(filepath.Join("/sys/fs/cgroup", group, "cgroup.events"))
	if errors.Is(err, os.ErrNotExist) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	for _, line := range strings.Split(string(data), "\n") {
		if value, ok := strings.CutPrefix(line, "populated "); ok {
			return value == "0", nil
		}
	}
	return false, errors.New("native cgroup.events has no populated state")
}

func (p *SystemdPolicy) identityProperties() []systemd.Property {
	return []systemd.Property{property("User", strconv.FormatUint(uint64(p.AgentUID), 10)), property("Group", strconv.FormatUint(uint64(p.AgentGID), 10)), property("SetLoginEnvironment", false), property("NoNewPrivileges", true), property("CapabilityBoundingSet", uint64(0)), property("AmbientCapabilities", uint64(0))}
}

func (p *SystemdPolicy) parentUnit() string {
	if p.lifetimeUnit != "" {
		return p.lifetimeUnit
	}
	return runnerUnit
}
