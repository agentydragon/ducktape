package supervision

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
)

func TestNativeExitStatusRequiresRetainedInvocationEvidence(t *testing.T) {
	for _, test := range []struct {
		name        string
		unit        SystemdUnitSnapshot
		invocation  string
		wantCode    int
		wantDone    bool
		wantFailure bool
	}{
		{name: "normal exit", unit: SystemdUnitSnapshot{LoadState: "loaded", InvocationID: "current", ExecMainCode: 1, ExecMainStatus: 23}, invocation: "current", wantCode: 23, wantDone: true},
		{name: "signal exit", unit: SystemdUnitSnapshot{LoadState: "loaded", InvocationID: "current", ExecMainCode: 2, ExecMainStatus: 9}, invocation: "current", wantCode: 137, wantDone: true},
		{name: "still running", unit: SystemdUnitSnapshot{LoadState: "loaded", InvocationID: "current", MainPID: 42}, invocation: "current", wantCode: -1},
		{name: "collected status", unit: SystemdUnitSnapshot{LoadState: "not-found"}, invocation: "current", wantCode: -1, wantFailure: true},
		{name: "different invocation", unit: SystemdUnitSnapshot{LoadState: "loaded", InvocationID: "successor", ExecMainCode: 1}, invocation: "current", wantCode: -1, wantFailure: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			code, done, err := nativeExitStatus(test.unit, test.invocation)
			if code != test.wantCode || done != test.wantDone || (err != nil) != test.wantFailure {
				t.Fatalf("nativeExitStatus() = (%d, %t, %v), want (%d, %t, failure=%t)", code, done, err, test.wantCode, test.wantDone, test.wantFailure)
			}
		})
	}
}

func TestSystemdStopTimeoutUsesCallerGrace(t *testing.T) {
	for _, test := range []struct {
		grace time.Duration
		want  uint64
	}{
		{grace: 0, want: 1},
		{grace: time.Nanosecond, want: 1},
		{grace: 1501 * time.Nanosecond, want: 2},
		{grace: 50 * time.Millisecond, want: 50_000},
		{grace: stopGrace, want: 5_000_000},
	} {
		if got := systemdStopTimeout(test.grace); got != test.want {
			t.Errorf("systemdStopTimeout(%s) = %d, want %d", test.grace, got, test.want)
		}
	}
}

func TestNativeSystemdStopRequestSetsGraceAndReusesJob(t *testing.T) {
	job := make(chan string, 1)
	var operations []string
	backend := &systemdBackend{
		name: "native-test.service",
		setStopTimeout: func(_ context.Context, _ *systemd.Conn, name string, usec uint64) error {
			operations = append(operations, fmt.Sprintf("timeout:%s:%d", name, usec))
			return nil
		},
		stopUnit: func(_ context.Context, _ *systemd.Conn, name string) (<-chan string, error) {
			operations = append(operations, "stop:"+name)
			return job, nil
		},
	}
	grace := 50 * time.Millisecond
	first, err := backend.requestStop(context.Background(), nil, &grace)
	if err != nil {
		t.Fatal(err)
	}
	second, err := backend.requestStop(context.Background(), nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if first != job || second != first {
		t.Fatalf("stop completion channels first=%p second=%p, want same job %p", first, second, job)
	}
	want := []string{"timeout:native-test.service:50000", "stop:native-test.service"}
	if len(operations) != len(want) {
		t.Fatalf("operations=%v, want %v", operations, want)
	}
	for i := range want {
		if operations[i] != want[i] {
			t.Fatalf("operations=%v, want %v", operations, want)
		}
	}
}

func TestNativeSystemdReferenceReconnectsAfterPinLoss(t *testing.T) {
	first := &fakeSystemdUnitReference{connected: false}
	second := &fakeSystemdUnitReference{connected: true}
	third := &fakeSystemdUnitReference{connected: true}
	opened := 0
	backend := &systemdBackend{
		name:           "native-test.service",
		unitReferences: []systemdUnitReference{first},
		referenceUnit: func(context.Context, string) (systemdUnitReference, error) {
			opened++
			if opened == 1 {
				return second, nil
			}
			return third, nil
		},
	}
	if err := backend.retainUnitReference(nil); err != nil {
		t.Fatal(err)
	}
	if opened != 1 || !first.closed || len(backend.unitReferences) != 1 || backend.unitReferences[0] != second {
		t.Fatalf("first reconnect pin: opened=%d refs=%v oldClosed=%t", opened, backend.unitReferences, first.closed)
	}
	second.connected = false
	if err := backend.retainUnitReference(nil); err != nil {
		t.Fatal(err)
	}
	if opened != 2 || !second.closed || len(backend.unitReferences) != 1 || backend.unitReferences[0] != third {
		t.Fatalf("second reconnect pin: opened=%d refs=%v oldClosed=%t", opened, backend.unitReferences, second.closed)
	}
}

type fakeSystemdUnitReference struct {
	connected bool
	closed    bool
}

func (r *fakeSystemdUnitReference) Connected() bool { return r.connected }

func (r *fakeSystemdUnitReference) Close() error {
	r.closed = true
	r.connected = false
	return nil
}

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

func TestSystemdFenceLostInvocationUsesOnlyCapturedEmptyDomain(t *testing.T) {
	groupReadError := errors.New("cgroup evidence unavailable")
	for _, test := range []struct {
		name      string
		snapshot  SystemdUnitSnapshot
		expected  SystemdFence
		empty     bool
		readErr   error
		wantError bool
		wantRead  bool
	}{
		{
			name:     "loaded stopped unit with empty captured domain",
			snapshot: SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "inactive"},
			expected: SystemdFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			empty:    true, wantRead: true,
		},
		{
			name:     "collected unit with empty captured domain",
			snapshot: SystemdUnitSnapshot{LoadState: "not-found"},
			expected: SystemdFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			empty:    true, wantRead: true,
		},
		{
			name:      "missing captured domain remains unknown",
			snapshot:  SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "inactive", ControlGroup: "/replacement"},
			expected:  SystemdFence{Ordered: true, InvocationID: "old"},
			wantError: true,
		},
		{
			name:      "populated captured domain remains unknown",
			snapshot:  SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "failed"},
			expected:  SystemdFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			wantError: true, wantRead: true,
		},
		{
			name:     "unreadable captured domain remains unknown",
			snapshot: SystemdUnitSnapshot{LoadState: "not-found"},
			expected: SystemdFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			readErr:  groupReadError, wantError: true, wantRead: true,
		},
		{
			name:      "live manager pid with lost identity remains unknown",
			snapshot:  SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "active", MainPID: 42},
			expected:  SystemdFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			wantError: true,
		},
		{
			name:      "outstanding manager job with lost identity remains unknown",
			snapshot:  SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "inactive", JobID: 9},
			expected:  SystemdFence{Ordered: true, InvocationID: "old", ControlGroup: "/old"},
			wantError: true,
		},
		{
			name:     "successful completion still requires invocation result",
			snapshot: SystemdUnitSnapshot{LoadState: "loaded", ActiveState: "inactive"},
			expected: SystemdFence{Ordered: true, InvocationID: "old", ControlGroup: "/old", RequireSuccess: true},
			empty:    true, wantError: true,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			m := &testSystemdManager{snapshots: []SystemdUnitSnapshot{test.snapshot}}
			reads := 0
			err := fenceSystemdUnit(context.Background(), m, "test.service", test.expected, func(group string) (bool, error) {
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

func TestSystemdFenceCapturesDomainForUncertainStartCleanup(t *testing.T) {
	m := &testSystemdManager{result: "done", snapshots: []SystemdUnitSnapshot{
		{LoadState: "loaded", ActiveState: "active", InvocationID: "started", ControlGroup: "/started", MainPID: 42},
		{LoadState: "loaded", ActiveState: "inactive"},
	}}
	var inspected string
	err := fenceSystemdUnit(context.Background(), m, "test.service", SystemdFence{Ordered: true}, func(group string) (bool, error) {
		inspected = group
		return true, nil
	})
	if err != nil || m.stops != 1 || inspected != "/started" {
		t.Fatalf("error=%v stops=%d inspected=%q", err, m.stops, inspected)
	}
}

func TestSystemdFenceRejectsSuccessorEvenWhenOldDomainIsEmpty(t *testing.T) {
	m := &testSystemdManager{snapshots: []SystemdUnitSnapshot{{
		LoadState: "loaded", ActiveState: "inactive", InvocationID: "successor", ControlGroup: "/successor",
	}}}
	reads := 0
	err := fenceSystemdUnit(context.Background(), m, "test.service", SystemdFence{
		Ordered: true, InvocationID: "old", ControlGroup: "/old",
	}, func(string) (bool, error) {
		reads++
		return true, nil
	})
	if err == nil || reads != 0 || m.stops != 0 {
		t.Fatalf("error=%v cgroup reads=%d stops=%d", err, reads, m.stops)
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
