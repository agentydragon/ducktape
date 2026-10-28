package runner

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"strconv"
	"time"

	"github.com/godbus/dbus/v5"
)

const (
	workloadSlice      = "agentplane-workloads.slice"
	executorSlice      = "agentplane-executors.slice"
	runnerUnit         = "agentplane-runner.service"
	systemdDestination = "org.freedesktop.systemd1"
	managerInterface   = "org.freedesktop.systemd1.Manager"
	unitInterface      = "org.freedesktop.systemd1.Unit"
	serviceInterface   = "org.freedesktop.systemd1.Service"
)

// VMProcessPolicy describes systemd-owned containment, independently of a launch.
// scope is assigned from durable owner identity before opening any Session store.
type VMProcessPolicy struct {
	Slice                          string
	AgentUID, AgentGID             uint32
	BlockDevices                   []string
	AggregateLimits, ProcessLimits ResourceLimits
	scope                          string
	lifetimeUnit                   string
	startTransient                 func(context.Context, *dbus.Conn, string, []systemdProperty) (dbus.ObjectPath, error)
}

func (p *VMProcessPolicy) validateProcessLimits(limits ResourceLimits) error {
	if err := limits.Validate(); err != nil {
		return err
	}
	if limits.MemoryMaxBytes > p.AggregateLimits.MemoryMaxBytes || limits.PidsMax > p.AggregateLimits.PidsMax || limits.CPUQuotaMicros > p.AggregateLimits.CPUQuotaMicros {
		return errors.New("process resource limits exceed aggregate limits")
	}
	return nil
}

type systemdProperty struct {
	Name  string
	Value dbus.Variant
}
type systemdAuxUnit struct {
	Name       string
	Properties []systemdProperty
}
type systemdExec struct {
	Path      string
	Arguments []string
	Flags     []string
}
type systemdDeviceLimit struct {
	Path  string
	Limit uint64
}
type systemdUnitProcess struct {
	Group   string
	PID     uint32
	Command string
}

func property(name string, value any) systemdProperty {
	return systemdProperty{name, dbus.MakeVariant(value)}
}

func (p *VMProcessPolicy) properties(limits ResourceLimits) ([]systemdProperty, error) {
	if err := p.validateProcessLimits(limits); err != nil {
		return nil, err
	}
	props := []systemdProperty{
		property("CPUQuotaPerSecUSec", uint64(limits.CPUQuotaMicros)*10), property("CPUQuotaPeriodUSec", cgroupCPUPeriodMicros),
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

// A direct authenticated manager peer avoids dbus-daemon's per-UID connection
// limit. Like go-systemd's NewSystemdConnectionContext, this must not send Hello.
func connectSystemd() (*dbus.Conn, error) {
	conn, err := dbus.Dial("unix:path=/run/systemd/private")
	if err != nil {
		return nil, err
	}
	if err := conn.Auth([]dbus.Auth{dbus.AuthExternal(strconv.Itoa(os.Getuid()))}); err != nil {
		_ = conn.Close()
		return nil, err
	}
	return conn, nil
}

func systemdManager(conn *dbus.Conn) dbus.BusObject {
	return conn.Object(systemdDestination, dbus.ObjectPath("/org/freedesktop/systemd1"))
}

func systemdCall(ctx context.Context, conn *dbus.Conn, method string, args ...any) *dbus.Call {
	return systemdManager(conn).CallWithContext(ctx, managerInterface+"."+method, 0, args...)
}

func systemdProperties(ctx context.Context, conn *dbus.Conn, name, iface string) (map[string]dbus.Variant, error) {
	var path dbus.ObjectPath
	if err := systemdCall(ctx, conn, "GetUnit", name).Store(&path); err != nil {
		return nil, err
	}
	var props map[string]dbus.Variant
	err := conn.Object(systemdDestination, path).CallWithContext(ctx, "org.freedesktop.DBus.Properties.GetAll", 0, iface).Store(&props)
	return props, err
}

func systemdMissing(err error) bool {
	var e dbus.Error
	return errors.As(err, &e) && (e.Name == "org.freedesktop.systemd1.NoSuchUnit" || e.Name == "org.freedesktop.systemd1.LoadFailed")
}

func (p *VMProcessPolicy) prepare(config Config) error {
	if config.Launch.Sandbox.UID == "" {
		return errors.New("VM supervision requires a Sandbox owner UID")
	}
	if p.Slice != workloadSlice && p.Slice != executorSlice {
		return errors.New("invalid native systemd slice")
	}
	if p.AgentUID == 0 || p.AgentGID == 0 {
		return errors.New("VM native services require a non-root identity")
	}
	if err := p.AggregateLimits.Validate(); err != nil {
		return err
	}
	if err := p.validateProcessLimits(p.ProcessLimits); err != nil {
		return err
	}
	sum := sha256.Sum256([]byte(config.StateDir + "\x00" + config.Launch.Sandbox.UID))
	p.scope = fmt.Sprintf("agentplane-native-%x-", sum[:8])
	conn, err := connectSystemd()
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	props, err := p.properties(p.AggregateLimits)
	if err != nil {
		return err
	}
	if err := systemdCall(ctx, conn, "SetUnitProperties", p.Slice, true, props).Err; err != nil {
		return err
	}
	var units []struct {
		Name, Description, LoadState, ActiveState, SubState, Following string
		Path                                                           dbus.ObjectPath
		JobID                                                          uint32
		JobType                                                        string
		JobPath                                                        dbus.ObjectPath
	}
	if err := systemdCall(ctx, conn, "ListUnitsByPatterns", []string{}, []string{p.scope + "*.service"}).Store(&units); err != nil {
		return err
	}
	for _, unit := range units {
		service, err := systemdProperties(ctx, conn, unit.Name, serviceInterface)
		if err != nil {
			return err
		}
		if service["Slice"].Value() != p.Slice {
			continue
		}
		if err := fenceSystemdUnit(ctx, conn, unit.Name); err != nil {
			return fmt.Errorf("fence prior native unit %s: %w", unit.Name, err)
		}
		_ = systemdCall(ctx, conn, "ResetFailedUnit", unit.Name).Err
	}
	return nil
}

// fenceSystemdUnit only returns after the entire unit execution domain is empty.
// Stop completion and a vanished main PID are insufficient evidence on their own.
func fenceSystemdUnit(ctx context.Context, conn *dbus.Conn, name string) error {
	if err := systemdCall(ctx, conn, "StopUnit", name, "replace").Err; err != nil && !systemdMissing(err) {
		return err
	}
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		props, err := systemdProperties(ctx, conn, name, unitInterface)
		if systemdMissing(err) {
			return nil
		}
		if err != nil {
			return err
		}
		state, _ := props["ActiveState"].Value().(string)
		if state == "inactive" || state == "failed" {
			var processes []systemdUnitProcess
			err = systemdCall(ctx, conn, "GetUnitProcesses", name).Store(&processes)
			if systemdMissing(err) {
				return nil
			}
			if err != nil {
				return err
			}
			if len(processes) == 0 {
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

func (p *VMProcessPolicy) identityProperties() []systemdProperty {
	return []systemdProperty{property("User", strconv.FormatUint(uint64(p.AgentUID), 10)), property("Group", strconv.FormatUint(uint64(p.AgentGID), 10)), property("SetLoginEnvironment", false), property("NoNewPrivileges", true), property("CapabilityBoundingSet", uint64(0)), property("AmbientCapabilities", uint64(0))}
}
