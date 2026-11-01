package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
)

type unitInvocation struct {
	ID             string
	ControlGroup   string
	RequireSuccess bool
}

func startSystemdUnit(ctx context.Context, name string) (unitInvocation, error) {
	conn, err := systemd.NewSystemdConnectionContext(context.Background())
	if err != nil {
		return unitInvocation{}, fmt.Errorf("connect systemd to start %s: %w", name, err)
	}
	defer conn.Close()
	// A unit left by a previous Runtime invocation must be fenced before the new
	// invocation can be accepted. Runtime retains the exact new InvocationID below.
	if err := fenceSystemdUnitOnConnection(ctx, conn, name, unitInvocation{}); err != nil {
		return unitInvocation{}, fmt.Errorf("fence previous %s invocation: %w", name, err)
	}
	completed := make(chan string, 1)
	if _, err := conn.StartUnitContext(ctx, name, "replace", completed); err != nil {
		fenceErr := fenceSystemdUnitOnConnection(context.Background(), conn, name, unitInvocation{})
		return unitInvocation{}, errors.Join(fmt.Errorf("start systemd unit %s: %w", name, err), fenceErr)
	}
	if err := waitSystemdJob(ctx, name, "start", completed); err != nil {
		fenceErr := fenceSystemdUnitOnConnection(context.Background(), conn, name, unitInvocation{})
		return unitInvocation{}, errors.Join(err, fenceErr)
	}
	invocation, state, pid, err := readSystemdInvocation(ctx, conn, name)
	if err != nil {
		fenceErr := fenceSystemdUnitOnConnection(context.Background(), conn, name, invocation)
		return unitInvocation{}, errors.Join(err, fenceErr)
	}
	if state != "active" || pid == 0 || invocation.ID == "" || invocation.ControlGroup == "" {
		fenceErr := fenceSystemdUnitOnConnection(context.Background(), conn, name, invocation)
		return unitInvocation{}, errors.Join(fmt.Errorf("systemd unit %s did not enter a verifiable live invocation (state=%s, main_pid=%d, invocation=%q, cgroup=%q)", name, state, pid, invocation.ID, invocation.ControlGroup), fenceErr)
	}
	invocation.RequireSuccess = true
	return invocation, nil
}

func fenceSystemdUnit(ctx context.Context, name string, expected unitInvocation) error {
	conn, err := systemd.NewSystemdConnectionContext(ctx)
	if err != nil {
		return fmt.Errorf("connect systemd to fence %s: %w", name, err)
	}
	defer conn.Close()
	return fenceSystemdUnitOnConnection(ctx, conn, name, expected)
}

func fenceSystemdUnitOnConnection(ctx context.Context, conn *systemd.Conn, name string, expected unitInvocation) error {
	service, serviceErr := conn.GetUnitTypePropertiesContext(ctx, name, "Service")
	if serviceErr != nil && !systemdUnitMissing(serviceErr) {
		return serviceErr
	}
	unit, unitErr := conn.GetUnitPropertiesContext(ctx, name)
	if unitErr != nil && !systemdUnitMissing(unitErr) {
		return unitErr
	}
	group := expected.ControlGroup
	currentID := systemdString(unit["InvocationID"])
	currentGroup := systemdString(service["ControlGroup"])
	activeState := systemdString(unit["ActiveState"])
	active := activeState != "inactive" && activeState != "failed" && activeState != ""
	missingInitially := systemdUnitMissing(unitErr) && systemdUnitMissing(serviceErr)
	if missingInitially && expected.ID == "" {
		return nil
	}
	// Packaged units are loaded before their first start, but have no cgroup until
	// systemd creates their first invocation.
	if unitErr == nil && serviceErr == nil && systemdUnitNeverStarted(unit, service, expected) {
		return nil
	}
	if active && currentGroup == "" && group == "" {
		return fmt.Errorf("systemd unit %s has no observable cgroup; cannot prove its process fence", name)
	}
	if expected.ID != "" && currentID != "" && currentID != expected.ID {
		if active {
			return fmt.Errorf("systemd unit %s invocation changed from expected %s to %s while active", name, expected.ID, currentID)
		}
		// Do not substitute a later invocation's cgroup for the exact invocation
		// whose fence the caller is waiting to acknowledge.
		group = expected.ControlGroup
	} else if active && expected.ID != "" && currentID == "" {
		return fmt.Errorf("systemd unit %s has no observable invocation ID while active", name)
	}
	if currentGroup != "" && (expected.ID == "" || currentID == expected.ID) {
		if group != "" && currentGroup != group && active {
			return fmt.Errorf("systemd unit %s changed cgroup while its prior invocation is still active", name)
		}
		group = currentGroup
	}
	if expected.ID != "" && group == "" {
		return fmt.Errorf("systemd unit %s has no recorded cgroup for invocation %s", name, expected.ID)
	}
	if systemdString(unit["LoadState"]) == "loaded" {
		if _, err := conn.StopUnitContext(ctx, name, "replace", nil); err != nil && !systemdUnitMissing(err) {
			return fmt.Errorf("stop systemd unit %s: %w", name, err)
		}
	}
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	for {
		unit, err := conn.GetUnitPropertiesContext(ctx, name)
		if err != nil && !systemdUnitMissing(err) {
			return fmt.Errorf("observe systemd unit %s while fencing: %w", name, err)
		}
		state := systemdString(unit["ActiveState"])
		if systemdUnitMissing(err) || state == "inactive" || state == "failed" {
			empty, err := systemdCgroupEmpty(group)
			if err != nil {
				return fmt.Errorf("inspect systemd unit %s cgroup: %w", name, err)
			}
			if empty {
				if expected.RequireSuccess {
					finalUnit, unitErr := conn.GetUnitPropertiesContext(ctx, name)
					if unitErr != nil {
						return fmt.Errorf("read completed systemd unit %s for %s: %w", name, expected.ID, unitErr)
					}
					finalService, serviceErr := conn.GetUnitTypePropertiesContext(ctx, name, "Service")
					if serviceErr != nil {
						return fmt.Errorf("read completed systemd invocation %s for %s: %w", expected.ID, name, serviceErr)
					}
					finalID := systemdString(finalUnit["InvocationID"])
					if finalID != "" && finalID != expected.ID {
						return fmt.Errorf("systemd unit %s completed invocation %s but now reports %s", name, expected.ID, finalID)
					}
					if !systemdInvocationSucceeded(finalService) {
						return fmt.Errorf("systemd unit %s invocation %s did not exit successfully (result=%q, code=%v, status=%v)", name, expected.ID, finalService["Result"], finalService["ExecMainCode"], finalService["ExecMainStatus"])
					}
				}
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

func readSystemdInvocation(ctx context.Context, conn *systemd.Conn, name string) (unitInvocation, string, uint32, error) {
	unit, err := conn.GetUnitPropertiesContext(ctx, name)
	if err != nil {
		return unitInvocation{}, "", 0, err
	}
	service, err := conn.GetUnitTypePropertiesContext(ctx, name, "Service")
	if err != nil {
		return unitInvocation{}, "", 0, err
	}
	pid, _ := service["MainPID"].(uint32)
	return unitInvocation{ID: systemdString(unit["InvocationID"]), ControlGroup: systemdString(service["ControlGroup"])}, systemdString(unit["ActiveState"]), pid, nil
}

func waitSystemdJob(ctx context.Context, name, verb string, completed <-chan string) error {
	select {
	case result := <-completed:
		if result != "done" {
			return fmt.Errorf("systemd %s job for %s completed with %q", verb, name, result)
		}
		return nil
	case <-ctx.Done():
		return fmt.Errorf("wait for systemd %s job for %s: %w", verb, name, ctx.Err())
	}
}

func systemdString(value any) string {
	switch value := value.(type) {
	case string:
		return value
	case []byte:
		return strings.TrimRight(string(value), "\x00")
	default:
		return ""
	}
}

// A loaded, inactive unit with no PID, invocation ID, or cgroup has never owned
// a process domain, so there is no previous invocation to fence.
func systemdUnitNeverStarted(unit, service map[string]any, expected unitInvocation) bool {
	return expected.ID == "" && expected.ControlGroup == "" && !expected.RequireSuccess &&
		systemdString(unit["LoadState"]) == "loaded" && systemdString(unit["ActiveState"]) == "inactive" &&
		systemdUint32(service["MainPID"]) == 0 && systemdString(unit["InvocationID"]) == "" && systemdString(service["ControlGroup"]) == ""
}

func systemdUnitMissing(err error) bool {
	var dbusErr dbus.Error
	return errors.As(err, &dbusErr) && (dbusErr.Name == "org.freedesktop.systemd1.NoSuchUnit" || dbusErr.Name == "org.freedesktop.systemd1.LoadFailed" || dbusErr.Name == "org.freedesktop.DBus.Error.UnknownObject")
}

func systemdCgroupEmpty(group string) (bool, error) {
	if group == "" {
		return false, errors.New("systemd cgroup path is required to prove an empty process domain")
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
	return false, errors.New("cgroup.events has no populated state")
}

func systemdInvocationSucceeded(service map[string]any) bool {
	// Result is systemd's authoritative outcome after the exact invocation's
	// cgroup has been fenced. ExecMainCode and ExecMainStatus may both be zero
	// when systemd stopped the unit without recording a main-process exit.
	return systemdString(service["Result"]) == "success"
}

func systemdUint32(value any) uint32 {
	switch value := value.(type) {
	case uint32:
		return value
	case uint64:
		return uint32(value)
	case int32:
		if value >= 0 {
			return uint32(value)
		}
	case int64:
		if value >= 0 {
			return uint32(value)
		}
	}
	return 0
}
