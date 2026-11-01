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
	runtimeUnit              = "agentplane-environment-runtime.service"
	runtimeLifetimeDirectory = "/run/agentplane-environment-runtime-lifetime"
)

// SystemdScopeID selects the process namespace used for stale-unit fencing.
type SystemdScopeID string

// SystemdRunnerScopeID preserves Runner's existing native process scope.
const SystemdRunnerScopeID SystemdScopeID = "runner"

// SystemdOwner identifies the service whose lifetime bounds native processes.
type SystemdOwner struct {
	Unit              string
	LifetimeDirectory string
	ScopeID           SystemdScopeID
}

// SystemdLauncher describes systemd-owned containment, independently of a launch.
// Prepare assigns scope from durable owner identity before any Session store opens.
type SystemdLauncher struct {
	Slice                          string
	AgentUID, AgentGID             uint32
	BlockDevices                   []string
	AggregateLimits, ProcessLimits ResourceLimits
	scope                          string
	lifetimeUnit                   string
	lifetimeMarker                 string
	startTransient                 func(context.Context, *systemd.Conn, string, []systemd.Property, chan<- string) (int, error)
	reapSnapshot                   func(context.Context, *systemd.Conn, string) (SystemdUnitSnapshot, error)
}

// ValidateProcessLimits checks values and ensures one process remains within its aggregate budget.
func (p *SystemdLauncher) ValidateProcessLimits(limits ResourceLimits) error {
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

func (p *SystemdLauncher) properties(limits ResourceLimits) ([]systemd.Property, error) {
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

// Prepare uses the environment Runtime's legacy native-process scope.
func (p *SystemdLauncher) Prepare(stateDir, sandboxUID string) (*Supervisor, error) {
	return p.PrepareForOwner(stateDir, sandboxUID, SystemdOwner{
		Unit: runtimeUnit, LifetimeDirectory: runtimeLifetimeDirectory, ScopeID: SystemdRunnerScopeID,
	})
}

// PrepareForOwner validates and applies the owner scope, fences units left by a
// prior owner incarnation, and returns a factory for processes in that scope.
func (p *SystemdLauncher) PrepareForOwner(stateDir, sandboxUID string, owner SystemdOwner) (*Supervisor, error) {
	if p == nil {
		return nil, errors.New("VM process launcher is required")
	}
	if err := owner.validate(); err != nil {
		return nil, err
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
	if err := p.prepareOwner(owner); err != nil {
		return nil, err
	}
	sum := sha256.Sum256([]byte(systemdScopeIdentity(stateDir, sandboxUID, owner)))
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
		service, err := ReadSystemdUnit(ctx, conn, unit.Name)
		if err != nil {
			return nil, err
		}
		if service.Slice != p.Slice {
			continue
		}
		if err := FenceSystemdUnit(ctx, conn, unit.Name, SystemdFence{Ordered: true}); err != nil {
			return nil, fmt.Errorf("fence prior native unit %s: %w", unit.Name, err)
		}
		_ = conn.ResetFailedUnitContext(ctx, unit.Name)
	}
	return p.newSupervisor(), nil
}

func systemdScopeIdentity(stateDir, sandboxUID string, owner SystemdOwner) string {
	identity := stateDir + "\x00" + sandboxUID
	if owner.ScopeID != SystemdRunnerScopeID {
		identity += "\x00" + string(owner.ScopeID)
	}
	return identity
}

func (owner SystemdOwner) validate() error {
	if owner.Unit == "" {
		return errors.New("VM supervision requires a systemd owner unit")
	}
	if !filepath.IsAbs(owner.LifetimeDirectory) {
		return errors.New("systemd owner lifetime directory must be absolute")
	}
	if owner.ScopeID == "" || strings.ContainsRune(string(owner.ScopeID), '\x00') {
		return errors.New("systemd owner scope ID is required and must not contain NUL")
	}
	return nil
}

func (p *SystemdLauncher) prepareOwner(owner SystemdOwner) error {
	marker, err := prepareSystemdLifetime(owner.LifetimeDirectory)
	if err != nil {
		return err
	}
	p.lifetimeUnit = owner.Unit
	p.lifetimeMarker = marker
	return nil
}

// newSupervisor creates a factory from an immutable snapshot of prepared launcher state.
func (p *SystemdLauncher) newSupervisor() *Supervisor {
	prepared := *p
	prepared.BlockDevices = append([]string(nil), p.BlockDevices...)
	return &Supervisor{makeBackend: func(config ProcessConfig) processBackend {
		launcher := prepared
		launcher.BlockDevices = append([]string(nil), prepared.BlockDevices...)
		return &systemdBackend{config: config, launcher: &launcher}
	}}
}

// systemd removes the owner directory synchronously when the service dies. An
// old queued launch therefore cannot cross exec after a successor starts.
func prepareSystemdLifetime(directory string) (string, error) {
	invocation := os.Getenv("INVOCATION_ID")
	if len(invocation) != 32 {
		return "", errors.New("VM supervision requires a systemd INVOCATION_ID")
	}
	if _, err := hex.DecodeString(invocation); err != nil {
		return "", fmt.Errorf("invalid systemd INVOCATION_ID: %w", err)
	}
	directoryInfo, err := os.Lstat(directory)
	if err != nil {
		return "", fmt.Errorf("inspect systemd owner lifetime directory: %w", err)
	}
	directoryOwner, ok := directoryInfo.Sys().(*syscall.Stat_t)
	if !directoryInfo.IsDir() || !ok || directoryOwner.Uid != 0 || directoryInfo.Mode().Perm()&0022 != 0 {
		return "", errors.New("systemd owner lifetime directory must be owned and writable only by root")
	}
	marker := filepath.Join(directory, invocation)
	file, err := os.OpenFile(marker, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0444)
	if err == nil {
		err = errors.Join(file.Chmod(0444), file.Close())
		if err != nil {
			return "", fmt.Errorf("create systemd owner lifetime marker: %w", err)
		}
	} else if !errors.Is(err, os.ErrExist) {
		return "", fmt.Errorf("create systemd owner lifetime marker: %w", err)
	}
	info, err := os.Lstat(marker)
	if err != nil {
		return "", err
	}
	markerOwner, ok := info.Sys().(*syscall.Stat_t)
	if !info.Mode().IsRegular() || !ok || markerOwner.Uid != 0 || info.Mode().Perm() != 0444 {
		return "", errors.New("systemd owner lifetime marker must be a root-owned read-only regular file")
	}
	return marker, nil
}

func (p *SystemdLauncher) identityProperties() []systemd.Property {
	return []systemd.Property{property("User", strconv.FormatUint(uint64(p.AgentUID), 10)), property("Group", strconv.FormatUint(uint64(p.AgentGID), 10)), property("SetLoginEnvironment", false), property("NoNewPrivileges", true), property("CapabilityBoundingSet", uint64(0)), property("AmbientCapabilities", uint64(0))}
}

func (p *SystemdLauncher) parentUnit() string {
	if p.lifetimeUnit != "" {
		return p.lifetimeUnit
	}
	return runtimeUnit
}
