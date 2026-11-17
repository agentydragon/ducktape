package systemd

import (
	"context"
	"testing"
	"time"

	goSystemd "github.com/coreos/go-systemd/v22/dbus"
)

func TestNativeExitStatusRequiresRetainedInvocationEvidence(t *testing.T) {
	for _, test := range []struct {
		name        string
		unit        UnitSnapshot
		invocation  string
		wantCode    int
		wantDone    bool
		wantFailure bool
	}{
		{name: "normal exit", unit: UnitSnapshot{LoadState: "loaded", InvocationID: "current", ExecMainCode: 1, ExecMainStatus: 23}, invocation: "current", wantCode: 23, wantDone: true},
		{name: "signal exit", unit: UnitSnapshot{LoadState: "loaded", InvocationID: "current", ExecMainCode: 2, ExecMainStatus: 9}, invocation: "current", wantCode: 137, wantDone: true},
		{name: "still running", unit: UnitSnapshot{LoadState: "loaded", InvocationID: "current", MainPID: 42}, invocation: "current", wantCode: -1},
		{name: "collected status", unit: UnitSnapshot{LoadState: "not-found"}, invocation: "current", wantCode: -1, wantFailure: true},
		{name: "different invocation", unit: UnitSnapshot{LoadState: "loaded", InvocationID: "successor", ExecMainCode: 1}, invocation: "current", wantCode: -1, wantFailure: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			code, done, err := nativeExitStatus(test.unit, test.invocation)
			if code != test.wantCode || done != test.wantDone || (err != nil) != test.wantFailure {
				t.Fatalf("nativeExitStatus() = (%d, %t, %v), want (%d, %t, failure=%t)", code, done, err, test.wantCode, test.wantDone, test.wantFailure)
			}
		})
	}
}

func TestSystemdStopTimeoutUsesConfiguredGrace(t *testing.T) {
	for _, test := range []struct {
		grace time.Duration
		want  uint64
	}{
		{grace: 0, want: 1},
		{grace: time.Nanosecond, want: 1},
		{grace: 1501 * time.Nanosecond, want: 2},
		{grace: 50 * time.Millisecond, want: 50_000},
		{grace: 5 * time.Second, want: 5_000_000},
	} {
		if got := systemdStopTimeout(test.grace); got != test.want {
			t.Errorf("systemdStopTimeout(%s) = %d, want %d", test.grace, got, test.want)
		}
	}
}

func TestNativeSystemdStopRequestReusesJob(t *testing.T) {
	job := make(chan string, 1)
	var operations []string
	backend := &Backend{config: BackendConfig{
		StopUnit: func(_ context.Context, _ *goSystemd.Conn, name string) (<-chan string, error) {
			operations = append(operations, "stop:"+name)
			return job, nil
		},
	}}
	handle := newStartHandle(backend, nil, nil, "native-test.service", nil, nil, nil)
	first, err := handle.requestStop(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	second, err := handle.requestStop(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if first == second {
		t.Fatal("each stop observer must receive its own replayable completion channel")
	}
	job <- "done"
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	requireNativeStopCompletion(t, ctx, first)
	third, err := handle.requestStop(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if third == first || third == second {
		t.Fatal("a retried fence must receive a fresh completion channel")
	}
	for _, observer := range []<-chan string{second, third} {
		requireNativeStopCompletion(t, ctx, observer)
	}
	want := []string{"stop:native-test.service"}
	if len(operations) != len(want) {
		t.Fatalf("operations=%v, want %v", operations, want)
	}
	for i := range want {
		if operations[i] != want[i] {
			t.Fatalf("operations=%v, want %v", operations, want)
		}
	}
}

func requireNativeStopCompletion(t *testing.T, ctx context.Context, completed <-chan string) {
	t.Helper()
	select {
	case result, ok := <-completed:
		if !ok || result != "done" {
			t.Fatalf("stop completion = (%q, %t), want (done, true)", result, ok)
		}
	case <-ctx.Done():
		t.Fatalf("stop completion: %v", ctx.Err())
	}
}

func TestNativeSystemdReferenceOutlivesSetupContext(t *testing.T) {
	reference := &fakeSystemdUnitReference{connected: true}
	var setupContext context.Context
	backend := &Backend{config: BackendConfig{
		ReferenceUnit: func(ctx context.Context, _ string) (UnitReference, error) {
			setupContext = ctx
			return reference, nil
		},
	}}
	handle := newStartHandle(backend, nil, nil, "native-test.service", nil, nil, nil)
	if err := handle.retainUnitReference(nil); err != nil {
		t.Fatal(err)
	}
	if setupContext == nil || setupContext.Err() == nil {
		t.Fatalf("reference setup context after retainUnitReference = %v, want canceled", setupContext)
	}
	if !reference.Connected() || reference.closed || len(handle.unitReferences) != 1 {
		t.Fatalf("retained references=%v connected=%t closed=%t; want one live reference", handle.unitReferences, reference.Connected(), reference.closed)
	}
	if err := handle.unitReferences[0].Close(); err != nil {
		t.Fatal(err)
	}
	if reference.Connected() || !reference.closed {
		t.Fatalf("after explicit Close: connected=%t closed=%t; want closed", reference.Connected(), reference.closed)
	}
}

func TestNativeSystemdReferenceReconnectsAfterPinLoss(t *testing.T) {
	first := &fakeSystemdUnitReference{connected: false}
	second := &fakeSystemdUnitReference{connected: true}
	third := &fakeSystemdUnitReference{connected: true}
	opened := 0
	backend := &Backend{config: BackendConfig{
		ReferenceUnit: func(context.Context, string) (UnitReference, error) {
			opened++
			if opened == 1 {
				return second, nil
			}
			return third, nil
		},
	}}
	handle := newStartHandle(backend, nil, nil, "native-test.service", nil, nil, nil)
	handle.unitReferences = []UnitReference{first}
	if err := handle.retainUnitReference(nil); err != nil {
		t.Fatal(err)
	}
	if opened != 1 || !first.closed || len(handle.unitReferences) != 1 || handle.unitReferences[0] != second {
		t.Fatalf("first reconnect pin: opened=%d refs=%v oldClosed=%t", opened, handle.unitReferences, first.closed)
	}
	second.connected = false
	if err := handle.retainUnitReference(nil); err != nil {
		t.Fatal(err)
	}
	if opened != 2 || !second.closed || len(handle.unitReferences) != 1 || handle.unitReferences[0] != third {
		t.Fatalf("second reconnect pin: opened=%d refs=%v oldClosed=%t", opened, handle.unitReferences, second.closed)
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
