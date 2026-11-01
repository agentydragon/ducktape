package supervision

import (
	"context"
	"errors"
	"testing"
)

type testSystemdManager struct {
	snapshots []SystemdUnitSnapshot
	stops     int
	result    string
}

func (m *testSystemdManager) snapshot(context.Context, string) (SystemdUnitSnapshot, error) {
	s := m.snapshots[0]
	if len(m.snapshots) > 1 {
		m.snapshots = m.snapshots[1:]
	}
	return s, nil
}

func (m *testSystemdManager) stop(context.Context, string) (<-chan string, error) {
	m.stops++
	done := make(chan string, 1)
	done <- m.result
	return done, nil
}

func TestSystemdFenceReleasedDomain(t *testing.T) {
	// Normal systemd stop retains InvocationID but clears ControlGroup once empty.
	// Runtime startup must be able to retire this already-stopped packaged unit.
	for _, state := range []string{"inactive", "failed"} {
		t.Run(state, func(t *testing.T) {
			m := &testSystemdManager{snapshots: []SystemdUnitSnapshot{{LoadState: "loaded", ActiveState: state, InvocationID: "old", Result: "success"}}}
			err := fenceSystemdUnit(context.Background(), m, "old-owner.service", SystemdFence{Ordered: true}, func(string) (bool, error) {
				t.Fatal("released manager domain must not invent a kernel path")
				return false, nil
			})
			if err != nil || m.stops != 0 {
				t.Fatalf("fence: %v; stop requests=%d", err, m.stops)
			}
		})
	}
}

func TestSystemdFenceEvidence(t *testing.T) {
	for _, test := range []struct {
		name      string
		snapshot  SystemdUnitSnapshot
		expected  SystemdFence
		wantError bool
	}{
		{name: "success without exact expected invocation", snapshot: SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "inactive", Result: "success"}, expected: SystemdFence{Ordered: true, RequireSuccess: true}, wantError: true},
		{name: "ordered absence", snapshot: SystemdUnitSnapshot{LoadState: "not-found"}, expected: SystemdFence{Ordered: true}},
		{name: "unresolved start absence", snapshot: SystemdUnitSnapshot{LoadState: "not-found"}, wantError: true},
		{name: "loaded UUID reconciles acceptance", snapshot: SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "inactive"}},
		{name: "later stopped invocation", snapshot: SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "inactive", InvocationID: "later"}, expected: SystemdFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"}, wantError: true},
		{name: "lost invocation", snapshot: SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "inactive"}, expected: SystemdFence{Ordered: true, InvocationID: "old"}, wantError: true},
		{name: "collected success unavailable", snapshot: SystemdUnitSnapshot{LoadState: "not-found"}, expected: SystemdFence{Ordered: true, InvocationID: "old", RequireSuccess: true}, wantError: true},
		{name: "successful exact invocation", snapshot: SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "inactive", InvocationID: "old", Result: "success"}, expected: SystemdFence{Ordered: true, InvocationID: "old", RequireSuccess: true}},
		{name: "failed exact invocation", snapshot: SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "failed", InvocationID: "old", Result: "timeout"}, expected: SystemdFence{Ordered: true, InvocationID: "old", RequireSuccess: true}, wantError: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			m := &testSystemdManager{snapshots: []SystemdUnitSnapshot{test.snapshot}}
			err := fenceSystemdUnit(context.Background(), m, "test-owner.service", test.expected, func(string) (bool, error) { return true, nil })
			if (err != nil) != test.wantError || m.stops != 0 {
				t.Fatalf("error=%v, stops=%d", err, m.stops)
			}
		})
	}
}

func TestSystemdFenceWaitsForSavedDomain(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := &testSystemdManager{result: "done", snapshots: []SystemdUnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "old", ControlGroup: "/old", MainPID: 42},
		{LoadState: "loaded", ActiveState: "inactive", InvocationID: "old", Result: "success"},
	}}
	inspected := 0
	err := fenceSystemdUnit(ctx, m, "test-owner.service", SystemdFence{Ordered: true}, func(group string) (bool, error) {
		inspected++
		if group != "/old" {
			t.Fatalf("kernel domain=%q", group)
		}
		cancel()
		return false, nil
	})
	if !errors.Is(err, context.Canceled) || inspected != 1 || m.stops != 1 {
		t.Fatalf("error=%v inspected=%d stops=%d", err, inspected, m.stops)
	}
}

func TestSystemdFenceFailedStopRemainsBlocked(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := &testSystemdManager{result: "failed", snapshots: []SystemdUnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "old", ControlGroup: "/old", MainPID: 42},
		{LoadState: "loaded", ActiveState: "failed", InvocationID: "old", ControlGroup: "/old", Result: "timeout"},
	}}
	err := fenceSystemdUnit(ctx, m, "test-owner.service", SystemdFence{Ordered: true}, func(string) (bool, error) { cancel(); return false, nil })
	if !errors.Is(err, context.Canceled) || m.stops != 1 {
		t.Fatalf("error=%v stops=%d", err, m.stops)
	}
}

func TestSystemdFenceDoesNotRetireSuccessor(t *testing.T) {
	m := &testSystemdManager{result: "done", snapshots: []SystemdUnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "old", ControlGroup: "/old", MainPID: 42},
		{LoadState: "loaded", ActiveState: "inactive", InvocationID: "successor", ControlGroup: "/old"},
	}}
	if err := fenceSystemdUnit(context.Background(), m, "test-owner.service", SystemdFence{Ordered: true}, func(string) (bool, error) { return true, nil }); err == nil || m.stops != 1 {
		t.Fatalf("error=%v stops=%d", err, m.stops)
	}
}

func TestDecodeSystemdUnitRejectsIncompleteEvidence(t *testing.T) {
	properties := map[string]any{
		"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "ControlGroup": "", "Result": "success", "Slice": "test.slice",
		"MainPID": uint32(0), "ControlPID": uint32(0), "ExecMainPID": uint32(42), "ExecMainCode": int32(1), "ExecMainStatus": int32(0),
		"InvocationID": []byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}, "Job": []any{uint32(0), "/"},
	}
	snapshot, err := decodeSystemdUnit(properties)
	if err != nil || snapshot.InvocationID != "0102030405060708090a0b0c0d0e0f10" {
		t.Fatalf("snapshot=%+v error=%v", snapshot, err)
	}
	for key, value := range properties {
		t.Run(key, func(t *testing.T) {
			delete(properties, key)
			if _, err := decodeSystemdUnit(properties); err == nil {
				t.Fatal("missing property became cleanup evidence")
			}
			properties[key] = value
		})
	}
	properties["ControlGroup"] = uint32(0)
	if _, err := decodeSystemdUnit(properties); err == nil {
		t.Fatal("malformed cgroup became cleanup evidence")
	}
}

func TestSystemdFenceFailedStopCanFinishCleanup(t *testing.T) {
	m := &testSystemdManager{result: "failed", snapshots: []SystemdUnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "old", ControlGroup: "/old", MainPID: 42},
		{LoadState: "loaded", ActiveState: "failed", InvocationID: "old", ControlGroup: "/old", Result: "timeout"},
	}}
	err := fenceSystemdUnit(context.Background(), m, "test-owner.service", SystemdFence{Ordered: true}, func(string) (bool, error) { return true, nil })
	if err != nil || m.stops != 1 {
		t.Fatalf("error=%v stops=%d", err, m.stops)
	}
}
