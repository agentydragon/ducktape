package supervision

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
)

// SystemdUnitSnapshot is one manager observation across the Unit and Service
// interfaces. Missing or malformed properties are errors, never cleanup evidence.
type SystemdUnitSnapshot struct {
	LoadState, ActiveState, SubState string
	InvocationID, ControlGroup       string
	MainPID, ControlPID, ExecMainPID uint32
	ExecMainCode, ExecMainStatus     int32
	Result, Slice                    string
	JobID                            uint32
}

func (s SystemdUnitSnapshot) stopped() bool {
	return s.LoadState == "not-found" || s.ActiveState == "inactive" || s.ActiveState == "failed"
}

// ReadSystemdUnit uses one GetAll call so an invocation change cannot splice a
// previous unit's identity together with its successor's process evidence.
func ReadSystemdUnit(ctx context.Context, conn *systemd.Conn, name string) (SystemdUnitSnapshot, error) {
	properties, err := conn.GetAllPropertiesContext(ctx, name)
	if systemdMissing(err) {
		return SystemdUnitSnapshot{LoadState: "not-found"}, nil
	}
	if err != nil {
		return SystemdUnitSnapshot{}, err
	}
	return decodeSystemdUnit(properties)
}

func decodeSystemdUnit(properties map[string]any) (SystemdUnitSnapshot, error) {
	var s SystemdUnitSnapshot
	// The manager may synthesize a not-found Unit after transient-unit collection.
	if err := systemdProperty(properties, "LoadState", &s.LoadState); err != nil {
		return s, err
	}
	if s.LoadState == "not-found" {
		return s, nil
	}
	for _, field := range []struct {
		name  string
		value *string
	}{
		{"ActiveState", &s.ActiveState}, {"SubState", &s.SubState}, {"ControlGroup", &s.ControlGroup}, {"Result", &s.Result}, {"Slice", &s.Slice},
	} {
		if err := systemdProperty(properties, field.name, field.value); err != nil {
			return s, err
		}
	}
	for _, field := range []struct {
		name  string
		value *uint32
	}{
		{"MainPID", &s.MainPID}, {"ControlPID", &s.ControlPID}, {"ExecMainPID", &s.ExecMainPID},
	} {
		if err := systemdProperty(properties, field.name, field.value); err != nil {
			return s, err
		}
	}
	if err := systemdProperty(properties, "ExecMainCode", &s.ExecMainCode); err != nil {
		return s, err
	}
	if err := systemdProperty(properties, "ExecMainStatus", &s.ExecMainStatus); err != nil {
		return s, err
	}
	id, ok := properties["InvocationID"].([]byte)
	if !ok || (len(id) != 0 && len(id) != 16) {
		return s, fmt.Errorf("invalid systemd InvocationID: %T", properties["InvocationID"])
	}
	if len(id) != 0 && string(id) != string(make([]byte, 16)) {
		s.InvocationID = hex.EncodeToString(id)
	}
	job, ok := properties["Job"].([]any)
	if !ok || len(job) != 2 {
		return s, fmt.Errorf("invalid systemd Job: %T", properties["Job"])
	}
	s.JobID, ok = job[0].(uint32)
	if !ok {
		return s, fmt.Errorf("invalid systemd job ID: %T", job[0])
	}
	return s, nil
}

func systemdProperty[T any](properties map[string]any, name string, target *T) error {
	value, ok := properties[name].(T)
	if !ok {
		return fmt.Errorf("invalid or missing systemd property %s: %T", name, properties[name])
	}
	*target = value
	return nil
}

// SystemdFence identifies the exact process domain being retired. Ordered means
// acceptance is known or this connection orders observation behind an uncertain
// start. Replacement-connection absence alone cannot settle an uncertain start.
type SystemdFence struct {
	InvocationID, ControlGroup string
	Ordered, RequireSuccess    bool
}

type systemdUnitManager interface {
	snapshot(context.Context, string) (SystemdUnitSnapshot, error)
	stop(context.Context, string) (<-chan string, error)
}

type systemdUnitConnection struct{ conn *systemd.Conn }

func (m systemdUnitConnection) snapshot(ctx context.Context, name string) (SystemdUnitSnapshot, error) {
	return ReadSystemdUnit(ctx, m.conn, name)
}

func (m systemdUnitConnection) stop(ctx context.Context, name string) (<-chan string, error) {
	return StopSystemdUnit(ctx, m.conn, name)
}

// StopSystemdUnit requests the manager's configured stop sequence. Its returned
// job completion is not proof that the process domain is empty.
func StopSystemdUnit(ctx context.Context, conn *systemd.Conn, name string) (<-chan string, error) {
	completed := make(chan string, 1)
	if _, err := conn.StopUnitContext(ctx, name, "replace", completed); err != nil {
		if systemdMissing(err) {
			close(completed)
			return completed, nil
		}
		return nil, err
	}
	return completed, nil
}

// WaitSystemdJob observes job completion; callers still need an execution-domain fence.
func WaitSystemdJob(ctx context.Context, name, verb string, completed <-chan string) error {
	select {
	case result, ok := <-completed:
		if ok && result != "done" {
			return fmt.Errorf("systemd %s job for %s completed with %q", verb, name, result)
		}
		return nil
	case <-ctx.Done():
		return fmt.Errorf("wait for systemd %s job for %s: %w", verb, name, ctx.Err())
	}
}

// FenceSystemdUnit joins manager stop and kernel cleanup. Systemd owns signal
// escalation and timeout; a failed stop with populated descendants stays blocked.
func FenceSystemdUnit(ctx context.Context, conn *systemd.Conn, name string, expected SystemdFence) error {
	return fenceSystemdUnit(ctx, systemdUnitConnection{conn}, name, expected, systemdGroupEmpty)
}

func fenceSystemdUnit(ctx context.Context, manager systemdUnitManager, name string, expected SystemdFence, groupEmpty func(string) (bool, error)) error {
	if expected.RequireSuccess && expected.InvocationID == "" {
		return errors.New("successful systemd completion requires an exact expected invocation")
	}
	current, err := manager.snapshot(ctx, name)
	if err != nil {
		return err
	}
	if !expected.Ordered && current.LoadState != "loaded" {
		return errors.New("native launch acceptance remains unknown after transport loss")
	}
	// Discovering a loaded UUID unit reconciles acceptance without replaying Start.
	if expected.InvocationID == "" {
		expected.InvocationID = current.InvocationID
	}
	// An ordered fence may begin without an invocation identity after an
	// uncertain StartUnit result. Capture the first observed domain together
	// with the identity it represents so later InvocationID loss can only be
	// resolved against this exact cgroup.
	if expected.ControlGroup == "" && current.InvocationID != "" && current.InvocationID == expected.InvocationID {
		expected.ControlGroup = current.ControlGroup
	}
	// Keep the caller's original cgroup separately from the last observed path.
	// If systemd has collected a stopped unit object, only this previously
	// captured path can identify the old process domain whose cleanup we prove.
	originalGroup := expected.ControlGroup
	group := originalGroup
	observe := func(s SystemdUnitSnapshot) (bool, error) {
		if expected.InvocationID != "" && s.InvocationID != "" && s.InvocationID != expected.InvocationID {
			return false, fmt.Errorf("systemd unit %s invocation changed from %s to %s", name, expected.InvocationID, s.InvocationID)
		}
		if s.ControlGroup != "" {
			if group != "" && group != s.ControlGroup {
				return false, fmt.Errorf("systemd unit %s changed cgroup from %s to %s", name, group, s.ControlGroup)
			}
			group = s.ControlGroup
		}
		if expected.InvocationID != "" && s.InvocationID == "" {
			// A normal stop retains InvocationID while its unit object remains
			// loaded, but GC/reload can discard that evidence. For cleanup-only
			// fences, the exact previously captured cgroup is still an independent
			// proof of the old domain once both manager state and kernel state are
			// quiescent. Never use a replacement object's cgroup for this proof.
			if expected.RequireSuccess || !s.stopped() || s.MainPID != 0 || s.ControlPID != 0 || s.JobID != 0 {
				return false, fmt.Errorf("systemd unit %s lost invocation %s before a stopped, job-free state", name, expected.InvocationID)
			}
			if originalGroup == "" {
				return false, fmt.Errorf("systemd unit %s lost invocation %s without a captured cgroup", name, expected.InvocationID)
			}
			empty, err := groupEmpty(originalGroup)
			if err != nil {
				return false, fmt.Errorf("inspect captured cgroup %s for systemd unit %s: %w", originalGroup, name, err)
			}
			if !empty {
				return false, fmt.Errorf("systemd unit %s lost invocation %s while captured cgroup %s remains populated", name, expected.InvocationID, originalGroup)
			}
			return true, nil
		}
		if !s.stopped() || s.MainPID != 0 || s.ControlPID != 0 || s.JobID != 0 {
			return false, nil
		}
		if group != "" {
			empty, err := groupEmpty(group)
			if err != nil || !empty {
				return false, err
			}
		} else if s.LoadState != "loaded" && s.LoadState != "not-found" {
			return false, fmt.Errorf("systemd unit %s has no observable process domain", name)
		}
		// In systemd v260.5 normal unit_maybe_release_cgroup only clears ControlGroup
		// after recursive emptiness, retaining it for D-state leftovers. A stopped,
		// loaded unit's explicitly cleared property is manager cleanup evidence.
		// https://github.com/systemd/systemd/blob/v260.5/src/core/cgroup.c
		if expected.RequireSuccess && (s.LoadState != "loaded" || s.InvocationID != expected.InvocationID || s.Result != "success") {
			return false, fmt.Errorf("systemd unit %s invocation %s has no successful completion evidence (load=%s, invocation=%s, result=%s, code=%d, status=%d)", name, expected.InvocationID, s.LoadState, s.InvocationID, s.Result, s.ExecMainCode, s.ExecMainStatus)
		}
		return true, nil
	}
	if done, err := observe(current); done || err != nil {
		return err
	}
	completed, err := manager.stop(ctx, name)
	if err != nil {
		return fmt.Errorf("stop systemd unit %s: %w", name, err)
	}
	// A failed stop can leave an unkillable descendant. Continue observing its
	// cgroup instead of treating the job result as completed process cleanup.
	jobErr := WaitSystemdJob(ctx, name, "stop", completed)
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		current, err = manager.snapshot(ctx, name)
		if err != nil {
			return errors.Join(jobErr, err)
		}
		if done, err := observe(current); done || err != nil {
			if done && !expected.RequireSuccess {
				return nil
			}
			return errors.Join(jobErr, err)
		}
		select {
		case <-ctx.Done():
			return errors.Join(jobErr, fmt.Errorf("systemd unit %s cleanup remains unproven (state=%s, cgroup=%q): %w", name, current.ActiveState, group, ctx.Err()))
		case <-ticker.C:
		}
	}
}

// cgroup.events accounts for the entire subtree. Empty paths are not kernel evidence.
func systemdGroupEmpty(group string) (bool, error) {
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
			switch value {
			case "0":
				return true, nil
			case "1":
				return false, nil
			default:
				return false, fmt.Errorf("invalid cgroup.events populated state %q", value)
			}
		}
	}
	return false, errors.New("cgroup.events has no populated state")
}

func systemdMissing(err error) bool {
	var e dbus.Error
	return errors.As(err, &e) && (e.Name == "org.freedesktop.systemd1.NoSuchUnit" || e.Name == "org.freedesktop.systemd1.LoadFailed" || e.Name == "org.freedesktop.DBus.Error.UnknownObject")
}
