package systemd

import (
	"context"
	"errors"
	"testing"
)

type testSystemdManager struct {
	snapshots []UnitSnapshot
	stops     int
	result    string
}

func (m *testSystemdManager) Snapshot(context.Context, string) (UnitSnapshot, error) {
	s := m.snapshots[0]
	if len(m.snapshots) > 1 {
		m.snapshots = m.snapshots[1:]
	}
	return s, nil
}

func (m *testSystemdManager) Stop(context.Context, string) (<-chan string, error) {
	m.stops++
	done := make(chan string, 1)
	done <- m.result
	return done, nil
}

func TestUnitFenceReleasedDomain(t *testing.T) {
	// Normal systemd stop retains InvocationID but clears ControlGroup once empty.
	// Runtime startup must be able to retire this already-stopped packaged unit.
	for _, state := range []string{"inactive", "failed"} {
		t.Run(state, func(t *testing.T) {
			m := &testSystemdManager{snapshots: []UnitSnapshot{{LoadState: "loaded", ActiveState: state, InvocationID: "old", Result: "success"}}}
			err := fenceUnit(context.Background(), m, "old-owner.service", UnitFence{Ordered: true}, func(string) (bool, error) {
				t.Fatal("released manager domain must not invent a kernel path")
				return false, nil
			})
			if err != nil || m.stops != 0 {
				t.Fatalf("fence: %v; stop requests=%d", err, m.stops)
			}
		})
	}
}

func TestUnitFenceEvidence(t *testing.T) {
	for _, test := range []struct {
		name      string
		snapshot  UnitSnapshot
		expected  UnitFence
		wantError bool
	}{
		{name: "ordered absence", snapshot: UnitSnapshot{LoadState: "not-found"}, expected: UnitFence{Ordered: true}},
		{name: "unresolved start absence", snapshot: UnitSnapshot{LoadState: "not-found"}, wantError: true},
		{name: "loaded UUID reconciles acceptance", snapshot: UnitSnapshot{LoadState: "loaded", ActiveState: "inactive"}},
		{name: "later stopped invocation", snapshot: UnitSnapshot{LoadState: "loaded", ActiveState: "inactive", InvocationID: "later"}, expected: UnitFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"}, wantError: true},
		{name: "lost invocation", snapshot: UnitSnapshot{LoadState: "loaded", ActiveState: "inactive"}, expected: UnitFence{Ordered: true, InvocationID: "old"}, wantError: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			m := &testSystemdManager{snapshots: []UnitSnapshot{test.snapshot}}
			err := fenceUnit(context.Background(), m, "test-owner.service", test.expected, func(string) (bool, error) { return true, nil })
			if (err != nil) != test.wantError || m.stops != 0 {
				t.Fatalf("error=%v, stops=%d", err, m.stops)
			}
		})
	}
}

func TestUnitFenceLostInvocationUsesOnlyCapturedEmptyDomain(t *testing.T) {
	groupReadError := errors.New("cgroup evidence unavailable")
	for _, test := range []struct {
		name      string
		snapshot  UnitSnapshot
		expected  UnitFence
		empty     bool
		readErr   error
		wantError bool
		wantRead  bool
	}{
		{
			name:     "loaded stopped unit with empty captured domain",
			snapshot: UnitSnapshot{LoadState: "loaded", ActiveState: "inactive"},
			expected: UnitFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			empty:    true, wantRead: true,
		},
		{
			name:     "collected unit with empty captured domain",
			snapshot: UnitSnapshot{LoadState: "not-found"},
			expected: UnitFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			empty:    true, wantRead: true,
		},
		{
			name:      "missing captured domain remains unknown",
			snapshot:  UnitSnapshot{LoadState: "loaded", ActiveState: "inactive", ControlGroup: "/replacement"},
			expected:  UnitFence{Ordered: true, InvocationID: "old"},
			wantError: true,
		},
		{
			name:      "populated captured domain remains unknown",
			snapshot:  UnitSnapshot{LoadState: "loaded", ActiveState: "failed"},
			expected:  UnitFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			wantError: true, wantRead: true,
		},
		{
			name:     "unreadable captured domain remains unknown",
			snapshot: UnitSnapshot{LoadState: "not-found"},
			expected: UnitFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			readErr:  groupReadError, wantError: true, wantRead: true,
		},
		{
			name:      "live manager pid with lost identity remains unknown",
			snapshot:  UnitSnapshot{LoadState: "loaded", ActiveState: "active", MainPID: 42},
			expected:  UnitFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			wantError: true,
		},
		{
			name:      "outstanding manager job with lost identity remains unknown",
			snapshot:  UnitSnapshot{LoadState: "loaded", ActiveState: "inactive", JobID: 9},
			expected:  UnitFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			wantError: true,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			m := &testSystemdManager{snapshots: []UnitSnapshot{test.snapshot}}
			reads := 0
			err := fenceUnit(context.Background(), m, "test.service", test.expected, func(group string) (bool, error) {
				reads++
				if group != test.expected.ControlGroup {
					t.Fatalf("inspected cgroup %q, want captured cgroup %q", group, test.expected.ControlGroup)
				}
				return test.empty, test.readErr
			})
			if (err != nil) != test.wantError || (reads > 0) != test.wantRead || m.stops != 0 {
				t.Fatalf("error=%v cgroup reads=%d stops=%d", err, reads, m.stops)
			}
			if test.readErr != nil && !errors.Is(err, test.readErr) {
				t.Fatalf("error=%v does not retain cgroup error %v", err, test.readErr)
			}
		})
	}
}

func TestUnitFenceCapturesDomainForUncertainStartCleanup(t *testing.T) {
	m := &testSystemdManager{result: "done", snapshots: []UnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "started", ControlGroup: "/started", MainPID: 42},
		{LoadState: "loaded", ActiveState: "inactive"},
	}}
	var inspected string
	err := fenceUnit(context.Background(), m, "test.service", UnitFence{Ordered: true}, func(group string) (bool, error) {
		inspected = group
		return true, nil
	})
	if err != nil || m.stops != 1 || inspected != "/started" {
		t.Fatalf("error=%v stops=%d inspected=%q", err, m.stops, inspected)
	}
}

func TestUnitFenceRejectsSuccessorEvenWhenOldDomainIsEmpty(t *testing.T) {
	m := &testSystemdManager{snapshots: []UnitSnapshot{{
		LoadState: "loaded", ActiveState: "inactive", InvocationID: "successor", ControlGroup: "/successor",
	}}}
	reads := 0
	err := fenceUnit(context.Background(), m, "test.service", UnitFence{
		Ordered: true, InvocationID: "old", ControlGroup: "/old",
	}, func(string) (bool, error) {
		reads++
		return true, nil
	})
	if err == nil || reads != 0 || m.stops != 0 {
		t.Fatalf("error=%v cgroup reads=%d stops=%d", err, reads, m.stops)
	}
}

func TestUnitFenceWaitsForSavedDomain(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := &testSystemdManager{result: "done", snapshots: []UnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "old", ControlGroup: "/old", MainPID: 42},
		{LoadState: "loaded", ActiveState: "inactive", InvocationID: "old", Result: "success"},
	}}
	inspected := 0
	err := fenceUnit(ctx, m, "test-owner.service", UnitFence{Ordered: true}, func(group string) (bool, error) {
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

func TestUnitFenceFailedStopRemainsBlocked(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := &testSystemdManager{result: "failed", snapshots: []UnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "old", ControlGroup: "/old", MainPID: 42},
		{LoadState: "loaded", ActiveState: "failed", InvocationID: "old", ControlGroup: "/old", Result: "timeout"},
	}}
	err := fenceUnit(ctx, m, "test-owner.service", UnitFence{Ordered: true}, func(string) (bool, error) { cancel(); return false, nil })
	if !errors.Is(err, context.Canceled) || m.stops != 1 {
		t.Fatalf("error=%v stops=%d", err, m.stops)
	}
}

func TestUnitFenceDoesNotRetireSuccessor(t *testing.T) {
	m := &testSystemdManager{result: "done", snapshots: []UnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "old", ControlGroup: "/old", MainPID: 42},
		{LoadState: "loaded", ActiveState: "inactive", InvocationID: "successor", ControlGroup: "/old"},
	}}
	if err := fenceUnit(context.Background(), m, "test-owner.service", UnitFence{Ordered: true}, func(string) (bool, error) { return true, nil }); err == nil || m.stops != 1 {
		t.Fatalf("error=%v stops=%d", err, m.stops)
	}
}

func TestDecodeSystemdUnitRejectsIncompleteEvidence(t *testing.T) {
	properties := map[string]any{
		"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "ControlGroup": "", "Result": "success", "Slice": "test.slice",
		"MainPID": uint32(0), "ControlPID": uint32(0), "ExecMainPID": uint32(42), "ExecMainCode": int32(1), "ExecMainStatus": int32(0),
		"InvocationID": []byte{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}, "Job": []any{uint32(0), "/"},
	}
	snapshot, err := decode(properties)
	if err != nil || snapshot.InvocationID != "0102030405060708090a0b0c0d0e0f10" {
		t.Fatalf("snapshot=%+v error=%v", snapshot, err)
	}
	for key, value := range properties {
		t.Run(key, func(t *testing.T) {
			delete(properties, key)
			if _, err := decode(properties); err == nil {
				t.Fatal("missing property became cleanup evidence")
			}
			properties[key] = value
		})
	}
	properties["ControlGroup"] = uint32(0)
	if _, err := decode(properties); err == nil {
		t.Fatal("malformed cgroup became cleanup evidence")
	}
}

func TestUnitFenceFailedStopCanFinishCleanup(t *testing.T) {
	m := &testSystemdManager{result: "failed", snapshots: []UnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "old", ControlGroup: "/old", MainPID: 42},
		{LoadState: "loaded", ActiveState: "failed", InvocationID: "old", ControlGroup: "/old", Result: "timeout"},
	}}
	err := fenceUnit(context.Background(), m, "test-owner.service", UnitFence{Ordered: true}, func(string) (bool, error) { return true, nil })
	if err != nil || m.stops != 1 {
		t.Fatalf("error=%v stops=%d", err, m.stops)
	}
}
