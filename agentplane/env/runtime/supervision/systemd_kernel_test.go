package supervision

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/godbus/dbus/v5"
	"github.com/google/uuid"
	"go.uber.org/zap"
)

const (
	fixtureStopGrace      = 5 * time.Second
	supervisionResultPath = "/state/testing/supervision/result.json"
)

type heldSystemdLaunch struct {
	name       string
	properties []systemd.Property
	release    chan bool
}

// This target runs before Runner in one ordinary VM guest. Session setup tests cover
// workspace creation, credentials, kernel budgets and detached-child fencing through
// the public protocol; these checks need private injection or direct supervisor access.
// It is deliberately excluded from supervision_test: no fake manager establishes these claims.
func TestSystemdKernelFixture(t *testing.T) {
	t.Setenv("AGENTPLANE_PARENT_ONLY_SENTINEL", "must not reach native services")
	if err := os.MkdirAll(filepath.Dir(supervisionResultPath), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(supervisionResultPath); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	config, sandboxUID := loadSystemdProofConfiguration(t)
	if config.Environment.Kind != "vm" {
		t.Fatal("systemd proof requires VM configuration")
	}
	launcher := &SystemdLauncher{
		Slice: WorkloadSlice, AgentUID: config.Environment.AgentUID, AgentGID: config.Environment.AgentGID,
		BlockDevices: config.Environment.BlockDevices, AggregateLimits: config.Environment.Resources.Aggregate,
		ProcessLimits: config.Environment.Resources.Process,
	}
	launcher.startTransient = func(c context.Context, bus *systemd.Conn, name string, props []systemd.Property, completion chan<- string) (int, error) {
		job, err := bus.StartTransientUnitContext(c, name, "fail", props, completion)
		t.Logf("native launch enqueue: unit=%s job=%d error=%v kind=%s", name, job, err, systemdErrorKind(err))
		return job, err
	}
	stateDir := "/state/testing/supervision/proof"
	runtimeOwner := SystemdOwner{
		Unit: "agentplane-environment-runtime.service", LifetimeDirectory: "/run/agentplane-environment-runtime-lifetime", ScopeID: SystemdEnvironmentRuntimeScopeID,
	}
	supervisor := reconcileKernelSystemdScope(t, launcher, stateDir, sandboxUID, runtimeOwner)
	// These cases deliberately inject stale markers and uncertain launch replies.
	if err := launcher.configureScope(stateDir, sandboxUID, runtimeOwner); err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp("/tmp", "systemd-proof-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	if err := os.Chmod(dir, 0777); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 70*time.Second)
	defer cancel()
	conn, cancelConnection, err := openSystemdConnection()
	if err != nil {
		t.Fatal(err)
	}
	defer cancelConnection()
	defer conn.Close()

	t.Run("stale_runner_invocation_cannot_execute", func(t *testing.T) {
		stale := *launcher
		stale.lifetimeMarker = filepath.Join(runtimeLifetimeDirectory, strings.ReplaceAll(uuid.NewString(), "-", ""))
		effect := filepath.Join(dir, "stale-runner-effect")
		p, err := stale.newSupervisor().NewProcess(ProcessConfig{Command: []string{"/bin/busybox", "touch", effect}, Dir: dir, Env: map[string]string{}, StopGrace: fixtureStopGrace})
		if err != nil {
			t.Fatal(err)
		}
		if err := p.Start(ctx); err == nil {
			t.Fatal("missing Runner invocation marker admitted a native process")
		}
		if _, err := p.Wait(ctx); err == nil {
			t.Fatal("skipped invocation start lost its error")
		}
		if p.PID() != 0 {
			t.Fatalf("skipped invocation published native PID %d", p.PID())
		}
		if _, err := os.Stat(effect); !os.IsNotExist(err) {
			t.Fatalf("stale invocation performed native effect: %v", err)
		}
		name := systemdProofUnitName(t, p)
		if name == "" {
			t.Fatal("stale invocation proof did not reach the manager")
		}
		assertSystemdProofEmpty(t, ctx, conn, name)
	})
	t.Run("held_old_invocation_is_fenced_after_successor_inventory", func(t *testing.T) {
		ownerSuffix := strings.ReplaceAll(uuid.NewString(), "-", "")
		ownerUnit := "agentplane-proof-owner-" + ownerSuffix + ".service"
		// The Runtime unit uses ProtectSystem=strict and allows writes only under
		// /run/agentplane/execution-service; keep this synthetic owner's lifetime
		// directory inside that dedicated writable subtree.
		directoryName := filepath.Join("agentplane", "execution-service", "proof-"+ownerSuffix)
		lifetimeDirectory := filepath.Join("/run", directoryName)
		owner := SystemdOwner{
			Unit: ownerUnit, LifetimeDirectory: lifetimeDirectory,
			ScopeID: SystemdScopeID("invocation-fence-" + ownerSuffix),
		}
		ownerProperties := []systemd.Property{
			property("Type", "exec"),
			property("ExecStartEx", []systemdExec{{"/bin/busybox", []string{"sleep", "600"}, []string{"no-env-expand"}}}),
			property("Restart", "on-failure"), property("RestartUSec", uint64(100_000)),
			property("RuntimeDirectory", []string{directoryName}), property("RuntimeDirectoryMode", uint32(0755)),
			property("RuntimeDirectoryPreserve", "no"), property("CollectMode", "inactive"),
		}
		t.Cleanup(func() {
			cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cleanupCancel()
			if err := FenceSystemdUnit(cleanupCtx, conn, ownerUnit, SystemdFence{Ordered: true}); err != nil {
				t.Errorf("fence synthetic owner: %v", err)
			}
		})
		if _, err := conn.StartTransientUnitContext(ctx, ownerUnit, "fail", ownerProperties, nil); err != nil {
			t.Fatal(err)
		}

		ownerA := waitSystemdInvocation(t, ctx, conn, ownerUnit, "", lifetimeDirectory)
		markerA := filepath.Join(lifetimeDirectory, ownerA.InvocationID)
		if len(ownerA.InvocationID) != 32 {
			t.Fatalf("synthetic owner has invalid invocation ID %q", ownerA.InvocationID)
		}
		// COSI's real scope reconciler reads the unit invocation identity from the
		// process environment. Supply the synthetic owner's manager-observed A ID.
		t.Setenv("INVOCATION_ID", ownerA.InvocationID)

		held := make(chan heldSystemdLaunch, 2)
		submittedProperties := make(chan []systemd.Property, 2)
		fixtureDone := make(chan struct{})
		var startDone []<-chan struct{}
		var heldProcesses []*Process
		t.Cleanup(func() {
			close(fixtureDone)
			cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 15*time.Second)
			defer cleanupCancel()
			startsSettled := true
			for _, done := range startDone {
				select {
				case <-done:
				case <-cleanupCtx.Done():
					t.Errorf("held systemd launch did not settle during cleanup: %v", cleanupCtx.Err())
					startsSettled = false
				}
			}
			if !startsSettled {
				return
			}
			for _, process := range heldProcesses {
				if process.Running() {
					if err := process.Stop(cleanupCtx); err != nil {
						t.Errorf("stop held synthetic process: %v", err)
					}
				}
			}
		})
		syntheticLauncher := &SystemdLauncher{
			Slice: launcher.Slice, AgentUID: launcher.AgentUID, AgentGID: launcher.AgentGID,
			BlockDevices:    append([]string(nil), launcher.BlockDevices...),
			AggregateLimits: launcher.AggregateLimits, ProcessLimits: launcher.ProcessLimits,
			startTransient: func(launchCtx context.Context, bus *systemd.Conn, name string, props []systemd.Property, completion chan<- string) (int, error) {
				launch := heldSystemdLaunch{
					name: name, properties: append([]systemd.Property(nil), props...), release: make(chan bool, 1),
				}
				held <- launch
				var omitConditions bool
				select {
				case omitConditions = <-launch.release:
				case <-launchCtx.Done():
					return 0, launchCtx.Err()
				case <-fixtureDone:
					return 0, context.Canceled
				}
				submitted := props
				if omitConditions {
					submitted = withoutSystemdProperty(props, "Conditions")
					if len(submitted) != len(props)-1 {
						return 0, errors.New("control launch did not remove exactly one Conditions property")
					}
				}
				submittedProperties <- append([]systemd.Property(nil), submitted...)
				return bus.StartTransientUnitContext(launchCtx, name, "fail", submitted, completion)
			},
		}
		stateDir := filepath.Join("/state/testing/supervision/invocation-fence", ownerSuffix)
		supervisorA := reconcileKernelSystemdScope(t, syntheticLauncher, stateDir, sandboxUID, owner)
		if _, err := os.Stat(markerA); err != nil {
			t.Fatalf("COSI scope reconciliation did not create owner A marker %s: %v", markerA, err)
		}

		startHeld := func(effect string) (*Process, <-chan error) {
			t.Helper()
			p, err := supervisorA.NewProcess(ProcessConfig{
				Command: []string{"/bin/sh", "-eu", "-c", `touch "$1"; exec /bin/busybox sleep 600`, "sh", effect},
				Dir:     dir, Env: map[string]string{"PATH": "/bin:/usr/bin"}, StopGrace: fixtureStopGrace,
			})
			if err != nil {
				t.Fatal(err)
			}
			heldProcesses = append(heldProcesses, p)
			result := make(chan error, 1)
			done := make(chan struct{})
			startDone = append(startDone, done)
			go func() {
				defer close(done)
				result <- p.Start(ctx)
			}()
			return p, result
		}
		receiveHeld := func() heldSystemdLaunch {
			t.Helper()
			select {
			case launch := <-held:
				return launch
			case <-ctx.Done():
				t.Fatalf("systemd launch was not held before deadline: %v", ctx.Err())
				return heldSystemdLaunch{}
			}
		}
		effectConditioned := filepath.Join(dir, "late-conditioned-effect")
		conditionedProcess, conditionedResult := startHeld(effectConditioned)
		conditioned := receiveHeld()
		effectControl := filepath.Join(dir, "late-no-condition-effect")
		controlProcess, controlResult := startHeld(effectControl)
		control := receiveHeld()

		assertHeldInvocationProperties(t, conditioned.properties, owner, markerA)
		assertHeldInvocationProperties(t, control.properties, owner, markerA)
		for _, launch := range []heldSystemdLaunch{conditioned, control} {
			unit, err := ReadSystemdUnit(ctx, conn, launch.name)
			if err != nil || unit.LoadState != "not-found" {
				t.Fatalf("held A unit %s exists before owner restart: unit=%+v err=%v", launch.name, unit, err)
			}
		}

		if err := conn.KillUnitWithTarget(ctx, ownerUnit, systemd.Main, int32(syscall.SIGKILL)); err != nil {
			t.Fatalf("kill synthetic owner A: %v", err)
		}
		ownerB := waitSystemdInvocation(t, ctx, conn, ownerUnit, ownerA.InvocationID, lifetimeDirectory)
		if ownerB.InvocationID == ownerA.InvocationID {
			t.Fatalf("synthetic owner restart reused invocation ID %q", ownerB.InvocationID)
		}
		waitForSystemdPathAbsent(t, ctx, markerA)
		markerB := filepath.Join(lifetimeDirectory, ownerB.InvocationID)
		if _, err := os.Stat(lifetimeDirectory); err != nil {
			t.Fatalf("synthetic owner B RuntimeDirectory is absent: %v", err)
		}
		t.Setenv("INVOCATION_ID", ownerB.InvocationID)
		reconcileKernelSystemdScope(t, syntheticLauncher, stateDir, sandboxUID, owner)
		if _, err := os.Stat(markerB); err != nil {
			t.Fatalf("successor COSI reconciliation did not create owner B marker %s: %v", markerB, err)
		}
		for _, launch := range []heldSystemdLaunch{conditioned, control} {
			unit, err := ReadSystemdUnit(ctx, conn, launch.name)
			if err != nil || unit.LoadState != "not-found" {
				t.Fatalf("successor COSI inventory did not observe held unit %s absent: unit=%+v err=%v", launch.name, unit, err)
			}
		}

		conditioned.release <- false
		if submitted := receiveSubmittedProperties(t, ctx, submittedProperties); !reflect.DeepEqual(conditioned.properties, submitted) {
			t.Fatal("conditioned launch properties changed before submission")
		}
		if err := receiveSystemdStartResult(t, ctx, conditionedResult); err == nil {
			t.Fatal("A invocation launch with its stale ConditionPathExists unexpectedly started after B inventory")
		}
		if _, err := conditionedProcess.Wait(ctx); err == nil {
			t.Fatal("skipped stale A process lost its start error")
		}
		if conditionedProcess.PID() != 0 {
			t.Fatalf("skipped stale A launch published PID %d", conditionedProcess.PID())
		}
		if _, err := os.Stat(effectConditioned); !os.IsNotExist(err) {
			t.Fatalf("stale A ConditionPathExists launch performed native effect: %v", err)
		}
		assertSystemdProofEmpty(t, ctx, conn, conditioned.name)

		control.release <- true
		if submitted := receiveSubmittedProperties(t, ctx, submittedProperties); !reflect.DeepEqual(withoutSystemdProperty(control.properties, "Conditions"), submitted) {
			t.Fatal("positive control changed properties other than Conditions")
		}
		if err := receiveSystemdStartResult(t, ctx, controlResult); err != nil {
			t.Fatalf("A control launch with only Conditions removed did not start under B: %v", err)
		}
		for {
			if _, err := os.Stat(effectControl); err == nil {
				break
			} else if !os.IsNotExist(err) {
				t.Fatalf("stat no-condition control effect: %v", err)
			}
			select {
			case <-ctx.Done():
				t.Fatalf("stale no-condition launch had no native effect: %v", ctx.Err())
			case <-time.After(20 * time.Millisecond):
			}
		}
		unitB, err := ReadSystemdUnit(ctx, conn, control.name)
		if err != nil || unitB.ActiveState != "active" || unitB.ExecMainPID == 0 {
			t.Fatalf("stale no-condition unit is not active under owner B: unit=%+v err=%v", unitB, err)
		}
		currentOwner, err := ReadSystemdUnit(ctx, conn, ownerUnit)
		if err != nil || currentOwner.ActiveState != "active" || currentOwner.InvocationID != ownerB.InvocationID {
			t.Fatalf("native control effect did not run while owner B was active: owner=%+v err=%v", currentOwner, err)
		}
		if err := controlProcess.Stop(ctx); err != nil {
			t.Fatalf("stop no-condition control process: %v", err)
		}
		if _, err := controlProcess.Wait(ctx); err != nil {
			t.Fatalf("wait for no-condition control stop: %v", err)
		}
		assertSystemdProofEmpty(t, ctx, conn, control.name)
	})

	t.Run("streams_and_environment", func(t *testing.T) {
		p := startSystemdProof(t, ctx, supervisor, dir, "printf 'out:%s:%s:%s\\n' \"$(id -u)\" \"$(id -g)\" \"$PWD\"; printf 'err:%s\\n' \"$SENTINEL\" >&2; printf 'home:%s\\n' \"$HOME\"; test -z \"${AGENTPLANE_PARENT_ONLY_SENTINEL+x}\"; IFS= read -r line; printf 'input:%s\\n' \"$line\"")
		var observationMu sync.Mutex
		var latest SystemdObservation
		p.ObserveSystemd(func(observation SystemdObservation) {
			observationMu.Lock()
			defer observationMu.Unlock()
			if observation.Revision > latest.Revision {
				latest = observation
			}
		})
		stdout, stderr := readProofStreams(p)
		unitName := systemdProofUnitName(t, p)
		if err := p.WriteLine(ctx, "verbatim input"); err != nil {
			t.Fatal(err)
		}
		code, err := p.Wait(ctx)
		if err != nil || code != 0 {
			t.Fatalf("wait code=%d err=%v", code, err)
		}
		observationMu.Lock()
		observed := latest
		observationMu.Unlock()
		if observed.Unit != unitName || observed.Revision < 2 || observed.StartState != SystemdStartAccepted || observed.ExpectedInvocationID == "" || observed.CapturedControlGroup == "" || observed.ExitState != SystemdExitObserved || observed.ExitCode != 0 {
			t.Fatalf("systemd observations did not retain attempt identity: %+v", observed)
		}
		want := fmt.Sprintf("out:%d:%d:%s\nhome:/explicit/proof-home\ninput:verbatim input\n", launcher.AgentUID, launcher.AgentGID, dir)
		if got := <-stdout; got != want {
			t.Fatalf("stdout=%q want %q", got, want)
		}
		if got := <-stderr; got != "err:proof sentinel\n" {
			t.Fatalf("stderr=%q", got)
		}
		assertSystemdProofEmpty(t, ctx, conn, unitName)
	})

	t.Run("argv_is_verbatim_and_only_shell_expands_environment", func(t *testing.T) {
		script := `printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "$4"; printf 'shell:%s:%s\n' "$HOME" "${SENTINEL}"`
		literals := []string{"$HOME", "${SENTINEL}", "%n", `single'quote"$`}
		command := append([]string{"/bin/sh", "-eu", "-c", script, "--"}, literals...)
		p, err := supervisor.NewProcess(ProcessConfig{Command: command, Dir: dir, Env: map[string]string{"PATH": "/bin:/usr/bin", "HOME": "/explicit/proof-home", "SENTINEL": "shell sentinel"}, StopGrace: fixtureStopGrace})
		if err != nil {
			t.Fatal(err)
		}
		if err := p.Start(ctx); err != nil {
			t.Fatal(err)
		}
		stdout, stderr := readProofStreams(p)
		if code, err := p.Wait(ctx); err != nil || code != 0 {
			t.Fatalf("verbatim argv exit=%d error=%v", code, err)
		}
		want := strings.Join(literals, "|") + "\nshell:/explicit/proof-home:shell sentinel\n"
		if got := <-stdout; got != want {
			t.Fatalf("argv/shell output=%q want %q", got, want)
		}
		if got := <-stderr; got != "" {
			t.Fatalf("argv/shell stderr=%q", got)
		}
	})
	t.Run("invalid_launch_cannot_escape_fence", func(t *testing.T) {
		p, err := supervisor.NewProcess(ProcessConfig{Command: []string{"/missing-agentplane-proof-binary"}, Dir: dir, Env: map[string]string{}, StopGrace: fixtureStopGrace})
		if err != nil {
			t.Fatal(err)
		}
		if err := p.Start(ctx); err == nil {
			t.Fatal("invalid native executable started")
		}
		if _, err := p.Wait(ctx); err == nil {
			t.Fatal("invalid start lost error")
		}
		unitName := systemdProofUnitName(t, p)
		if unitName == "" {
			t.Fatal("invalid launch never reached manager")
		}
		assertSystemdProofEmpty(t, ctx, conn, unitName)
	})
	t.Run("lost_reply_and_canceled_start", func(t *testing.T) {
		for _, cancelStart := range []bool{false, true} {
			owned := *launcher
			launchCtx, launchCancel := context.WithCancel(ctx)
			var launchName string
			launches := 0
			owned.startTransient = func(c context.Context, bus *systemd.Conn, name string, props []systemd.Property, completion chan<- string) (int, error) {
				launches++
				launchName = name
				job, err := bus.StartTransientUnitContext(c, name, "fail", props, completion)
				if err != nil {
					return job, err
				}
				if cancelStart {
					launchCancel()
					return job, nil
				}
				return job, context.DeadlineExceeded
			}
			p, err := owned.newSupervisor().NewProcess(ProcessConfig{Command: []string{"/bin/sh", "-c", "sleep 60"}, Dir: dir, Env: map[string]string{"PATH": "/bin:/usr/bin"}, StopGrace: fixtureStopGrace})
			if err != nil {
				t.Fatal(err)
			}
			if err := p.Start(launchCtx); err == nil {
				t.Fatal("uncertain/canceled launch unexpectedly succeeded")
			}
			launchCancel()
			if launches != 1 {
				t.Fatalf("issued %d launches", launches)
			}
			if _, err := p.Wait(ctx); err == nil {
				t.Fatal("failed start did not retain its error")
			}
			assertSystemdProofEmpty(t, ctx, conn, launchName)
		}
	})
	t.Run("startup_fences_owner_scope_only", func(t *testing.T) {
		ownedName := launcher.scope + uuid.NewString() + ".service"
		unrelatedName := "agentplane-proof-unrelated-" + uuid.NewString() + ".service"
		for _, name := range []string{ownedName, unrelatedName} {
			if _, err := conn.StartTransientUnitContext(ctx, name, "fail", proofUnitProperties("/bin/busybox", []string{"/bin/busybox", "sleep", "60"}, WorkloadSlice), nil); err != nil {
				t.Fatal(err)
			}
			defer FenceSystemdUnit(context.Background(), conn, name, SystemdFence{Ordered: true})
		}
		recovered := *launcher
		reconcileKernelSystemdScope(t, &recovered, stateDir, sandboxUID, runtimeOwner)
		assertSystemdProofEmpty(t, ctx, conn, ownedName)
		props, err := conn.GetUnitPropertiesContext(ctx, unrelatedName)
		if err != nil {
			t.Fatal(err)
		}
		if props["ActiveState"] != "active" {
			t.Fatalf("unrelated unit was stopped: %v", props["ActiveState"])
		}
	})
	t.Run("parent_death_graceful_cleanup", func(t *testing.T) {
		parent := "agentplane-proof-parent-" + uuid.NewString() + ".service"
		if _, err := conn.StartTransientUnitContext(ctx, parent, "fail", proofUnitProperties("/bin/busybox", []string{"/bin/busybox", "sleep", "60"}, "agentplane.slice"), nil); err != nil {
			t.Fatal(err)
		}
		defer FenceSystemdUnit(context.Background(), conn, parent, SystemdFence{Ordered: true})
		owned := *launcher
		owned.lifetimeUnit = parent
		marker := filepath.Join(dir, "saved")
		ownedSupervisor := owned.newSupervisor()
		p := startSystemdProof(t, ctx, ownedSupervisor, dir, "trap 'printf saved > "+marker+"; exit 0' TERM; printf ready; while :; do sleep 1; done")
		ready := make([]byte, 5)
		if _, err := io.ReadFull(p.Stdout(), ready); err != nil {
			t.Fatal(err)
		}
		if string(ready) != "ready" {
			t.Fatalf("readiness=%q", ready)
		}
		stdout, stderr := readProofStreams(p)
		if err := conn.KillUnitWithTarget(ctx, parent, systemd.Main, int32(9)); err != nil {
			t.Fatal(err)
		}
		if _, err := p.Wait(ctx); err != nil {
			t.Fatal(err)
		}
		<-stdout
		<-stderr
		data, err := os.ReadFile(marker)
		if err != nil || string(data) != "saved" {
			t.Fatalf("graceful persistence marker=%q err=%v", data, err)
		}
		assertSystemdProofEmpty(t, ctx, conn, systemdProofUnitName(t, p))
	})

	t.Run("ordinary_stop_retains_graceful_exit_status", func(t *testing.T) {
		p := startSystemdProof(t, ctx, supervisor, dir, "trap 'exit 23' TERM; printf ready; while :; do sleep 1; done")
		ready := make([]byte, 5)
		if _, err := io.ReadFull(p.Stdout(), ready); err != nil {
			t.Fatal(err)
		}
		stdout, stderr := readProofStreams(p)
		if err := p.Stop(ctx); err != nil {
			t.Fatal(err)
		}
		if code, err := p.Wait(ctx); err != nil || code != 23 {
			t.Fatalf("gracefulstop exit=%d error=%v want23", code, err)
		}
		<-stdout
		<-stderr
		assertSystemdProofEmpty(t, ctx, conn, systemdProofUnitName(t, p))
	})
	t.Run("ordinary_stop_waits_for_domain", func(t *testing.T) {
		p := startSystemdProofWithGrace(t, ctx, supervisor, dir, 50*time.Millisecond, "trap '' TERM; printf ready; sleep 60 & wait")
		ready := make([]byte, 5)
		if _, err := io.ReadFull(p.Stdout(), ready); err != nil {
			t.Fatal(err)
		}
		stdout, stderr := readProofStreams(p)
		stopStarted := time.Now()
		if err := p.Stop(ctx); err != nil {
			t.Fatal(err)
		}
		if elapsed := time.Since(stopStarted); elapsed > 3*time.Second {
			t.Fatalf("systemd stop ignored caller's 50ms grace: elapsed=%s", elapsed)
		}
		if code, err := p.Wait(ctx); err != nil || code != 137 {
			t.Fatalf("forcedstop exit=%d error=%v want137", code, err)
		}
		<-stdout
		<-stderr
		assertSystemdProofEmpty(t, ctx, conn, systemdProofUnitName(t, p))
	})
	t.Run("reap_retries_property_deadline_on_connected_manager", func(t *testing.T) {
		owned := *launcher
		authoritativePID := make(chan uint32, 1)
		propertyReads := 0 // Only this process's reaper calls the hook.
		owned.reapSnapshot = func(readCtx context.Context, bus *systemd.Conn, name string) (SystemdUnitSnapshot, error) {
			propertyReads++
			if propertyReads == 1 {
				return SystemdUnitSnapshot{}, context.DeadlineExceeded
			}
			props, err := ReadSystemdUnit(readCtx, bus, name)
			if err == nil {
				select {
				case authoritativePID <- props.MainPID:
				default:
				}
			}
			return props, err
		}
		p := startSystemdProof(t, ctx, owned.newSupervisor(), dir, "IFS= read -r line; printf 'input:%s\\n' \"$line\"; exit 23")
		stdout, stderr := readProofStreams(p)
		readCtx, cancelRead := context.WithTimeout(ctx, 10*time.Second)
		defer cancelRead()

		type waitResult struct {
			code int
			err  error
		}
		waited := make(chan waitResult, 1)
		go func() {
			code, err := p.Wait(readCtx)
			waited <- waitResult{code: code, err: err}
		}()
		select {
		case mainPID := <-authoritativePID:
			if int(mainPID) != p.PID() {
				t.Fatalf("authoritative MainPID=%d after retry, want original PID %d", mainPID, p.PID())
			}
		case result := <-waited:
			t.Fatalf("Wait completed instead of retrying the connected manager read: code=%d err=%v", result.code, result.err)
		case <-readCtx.Done():
			t.Fatalf("reaper did not retry the real manager read: %v", readCtx.Err())
		}
		if !p.Running() {
			t.Fatal("native process stopped before intentional stdin input")
		}
		select {
		case result := <-waited:
			t.Fatalf("Wait completed before intentional stdin input: code=%d err=%v", result.code, result.err)
		default:
		}

		if err := p.WriteLine(readCtx, "finish"); err != nil {
			t.Fatal(err)
		}
		select {
		case result := <-waited:
			if result.err != nil || result.code != 23 {
				t.Fatalf("Wait after intentional stdin input: code=%d err=%v, want 23", result.code, result.err)
			}
		case <-readCtx.Done():
			t.Fatalf("native process did not exit after intentional stdin input: %v", readCtx.Err())
		}
		if got := <-stdout; got != "input:finish\n" {
			t.Fatalf("native stdin/stdout round trip=%q", got)
		}
		<-stderr
		assertSystemdProofEmpty(t, readCtx, conn, systemdProofUnitName(t, p))
	})
	if t.Failed() {
		return
	}
	if err := os.WriteFile(supervisionResultPath, []byte("{\"real_systemd_supervision\":\"passed\"}\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(supervisionResultPath, 0644); err != nil {
		t.Fatal(err)
	}
}

func startSystemdProof(t *testing.T, ctx context.Context, supervisor *Supervisor, dir, script string) *Process {
	return startSystemdProofWithGrace(t, ctx, supervisor, dir, fixtureStopGrace, script)
}

func startSystemdProofWithGrace(t *testing.T, ctx context.Context, supervisor *Supervisor, dir string, grace time.Duration, script string) *Process {
	t.Helper()
	p, err := supervisor.NewProcess(ProcessConfig{Command: []string{"/bin/sh", "-eu", "-c", script}, Dir: dir, Env: map[string]string{"PATH": "/bin:/usr/bin", "HOME": "/explicit/proof-home", "SENTINEL": "proof sentinel"}, StopGrace: grace})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Start(ctx); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if p.Running() {
			_ = p.Stop(context.Background())
		}
	})
	return p
}

func readProofStreams(p *Process) (<-chan string, <-chan string) {
	stdout, stderr := make(chan string, 1), make(chan string, 1)
	go func() { data, _ := io.ReadAll(p.Stdout()); stdout <- string(data) }()
	go func() { data, _ := io.ReadAll(p.Stderr()); stderr <- string(data) }()
	return stdout, stderr
}

func assertHeldInvocationProperties(t *testing.T, properties []systemd.Property, owner SystemdOwner, marker string) {
	t.Helper()
	conditions, ok := systemdPropertyValue(t, properties, "Conditions").([]systemdCondition)
	if !ok || len(conditions) != 1 || conditions[0].Type != "ConditionPathExists" || conditions[0].Parameter != marker {
		t.Fatalf("held launch Conditions=%#v, want ConditionPathExists=%s", conditions, marker)
	}
	for _, name := range []string{"BindsTo", "After"} {
		value, ok := systemdPropertyValue(t, properties, name).([]string)
		if !ok || !reflect.DeepEqual(value, []string{owner.Unit}) {
			t.Fatalf("held launch %s=%#v, want stable owner name %q", name, value, owner.Unit)
		}
	}
}

func systemdPropertyValue(t *testing.T, properties []systemd.Property, name string) any {
	t.Helper()
	var value any
	count := 0
	for _, property := range properties {
		if property.Name == name {
			value = property.Value.Value()
			count++
		}
	}
	if count != 1 {
		t.Fatalf("launch properties contain %d %s entries, want exactly one", count, name)
	}
	return value
}

func withoutSystemdProperty(properties []systemd.Property, name string) []systemd.Property {
	filtered := make([]systemd.Property, 0, len(properties))
	for _, property := range properties {
		if property.Name != name {
			filtered = append(filtered, property)
		}
	}
	return filtered
}

func receiveSystemdStartResult(t *testing.T, ctx context.Context, result <-chan error) error {
	t.Helper()
	select {
	case err := <-result:
		return err
	case <-ctx.Done():
		t.Fatalf("wait for held systemd start result: %v", ctx.Err())
		return ctx.Err()
	}
}

func receiveSubmittedProperties(t *testing.T, ctx context.Context, submitted <-chan []systemd.Property) []systemd.Property {
	t.Helper()
	select {
	case properties := <-submitted:
		return properties
	case <-ctx.Done():
		t.Fatalf("wait for properties submitted to systemd: %v", ctx.Err())
		return nil
	}
}

func waitSystemdInvocation(t *testing.T, ctx context.Context, conn *systemd.Conn, name, previousInvocation, runtimeDirectory string) SystemdUnitSnapshot {
	t.Helper()
	timer := time.NewTimer(15 * time.Second)
	defer timer.Stop()
	ticker := time.NewTicker(25 * time.Millisecond)
	defer ticker.Stop()
	for {
		unit, err := ReadSystemdUnit(ctx, conn, name)
		if err != nil {
			t.Fatalf("read synthetic owner %s: %v", name, err)
		}
		if unit.ActiveState == "active" && unit.MainPID != 0 && len(unit.InvocationID) == 32 && unit.InvocationID != previousInvocation {
			if _, err := os.Stat(runtimeDirectory); err == nil {
				return unit
			}
		}
		select {
		case <-ctx.Done():
			t.Fatalf("wait for synthetic owner %s invocation (previous=%s): %v", name, previousInvocation, ctx.Err())
		case <-timer.C:
			t.Fatalf("timed out waiting for synthetic owner %s invocation (previous=%s); latest=%+v", name, previousInvocation, unit)
		case <-ticker.C:
		}
	}
}

func waitForSystemdPathAbsent(t *testing.T, ctx context.Context, path string) {
	t.Helper()
	timer := time.NewTimer(10 * time.Second)
	defer timer.Stop()
	ticker := time.NewTicker(25 * time.Millisecond)
	defer ticker.Stop()
	for {
		_, err := os.Stat(path)
		if os.IsNotExist(err) {
			return
		}
		if err != nil {
			t.Fatalf("inspect old invocation marker %s: %v", path, err)
		}
		select {
		case <-ctx.Done():
			t.Fatalf("wait for old invocation marker %s removal: %v", path, ctx.Err())
		case <-timer.C:
			t.Fatalf("old invocation marker %s survived owner restart", path)
		case <-ticker.C:
		}
	}
}

func assertSystemdProofEmpty(t *testing.T, ctx context.Context, conn *systemd.Conn, name string) {
	t.Helper()
	unit, err := ReadSystemdUnit(ctx, conn, name)
	if err != nil {
		t.Fatal(err)
	}
	if !unit.stopped() {
		t.Fatalf("unit %s still %s", name, unit.ActiveState)
	}
	if unit.ControlGroup == "" {
		return
	}
	if empty, err := systemdGroupEmpty(unit.ControlGroup); err != nil || !empty {
		t.Fatalf("unit %s kernel domain still populated: empty=%v err=%v", name, empty, err)
	}
}

func proofUnitProperties(executable string, args []string, slice string) []systemd.Property {
	return []systemd.Property{property("Type", "exec"), property("ExecStartEx", []systemdExec{{executable, args, []string{"no-env-expand"}}}), property("Slice", slice), property("RemainAfterExit", true), property("CollectMode", "inactive"), property("KillMode", "control-group"), property("TimeoutStopUSec", uint64(fixtureStopGrace/time.Microsecond))}
}

type systemdProofRunnerConfig struct {
	StateDir    string `json:"state_dir"`
	Environment struct {
		Kind         string   `json:"kind"`
		AgentUID     uint32   `json:"agent_uid"`
		AgentGID     uint32   `json:"agent_gid"`
		BlockDevices []string `json:"block_devices"`
		Resources    struct {
			Aggregate ResourceLimits `json:"aggregate"`
			Process   ResourceLimits `json:"process"`
		} `json:"resources"`
	} `json:"environment"`
}

func loadSystemdProofConfiguration(t *testing.T) (systemdProofRunnerConfig, string) {
	t.Helper()
	var config systemdProofRunnerConfig
	readSystemdProofJSON(t, "/run/agentplane/config/environment-runtime.json", &config)
	if config.StateDir == "" {
		t.Fatal("Runner configuration has no state_dir")
	}
	var launch struct {
		Sandbox struct {
			UID string `json:"uid"`
		} `json:"sandbox"`
	}
	readSystemdProofJSON(t, "/run/agentplane/sandbox/launch.json", &launch)
	if launch.Sandbox.UID == "" {
		t.Fatal("Sandbox launch has no owner UID")
	}
	return config, launch.Sandbox.UID
}

func readSystemdProofJSON(t *testing.T, path string, target any) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, target); err != nil {
		t.Fatalf("decode %s: %v", path, err)
	}
}

func systemdProofUnitName(t *testing.T, process *Process) string {
	t.Helper()
	backend, ok := process.backend.(*systemdBackend)
	if !ok {
		t.Fatalf("process backend = %T, want *systemdBackend", process.backend)
	}
	return backend.name
}

func systemdErrorKind(err error) string {
	var remote dbus.Error
	if errors.As(err, &remote) {
		return "dbus_error=" + remote.Name
	}
	return fmt.Sprintf("error_type=%T", err)
}

func reconcileKernelSystemdScope(t *testing.T, launcher *SystemdLauncher, stateDir, sandboxUID string, owner SystemdOwner) *Supervisor {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 45*time.Second)
	defer cancel()
	store := state.WrapCore(namespaced.NewState(inmem.Build))
	var workers sync.WaitGroup
	host := NewHost(ctx, store, "systemd-kernel-proof", RuntimeDomain, &workers, func(err error) { t.Error(err) })
	supervisor, err := host.DeclareSystemdScope(ctx, launcher, stateDir, sandboxUID, owner)
	if err != nil {
		t.Fatal(err)
	}
	engine, err := runtime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(host.SystemdScopeController()); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- engine.Run(ctx) }()
	_, err = store.WatchFor(ctx, resourceFor(host.namespace, string(owner.ScopeID), SystemdScopeStatus{}).Metadata(), state.WithCondition(func(res resource.Resource) (bool, error) {
		status, _, err := ReadSystemdScopeStatus(ctx, store, host.namespace, owner.ScopeID)
		return status.Ready || status.Error != "", err
	}))
	if err == nil {
		status, found, statusErr := ReadSystemdScopeStatus(ctx, store, host.namespace, owner.ScopeID)
		if statusErr != nil {
			err = fmt.Errorf("read COSI systemd scope status: %w", statusErr)
		} else if !found {
			err = errors.New("COSI systemd scope status was not published")
		} else if !status.Ready {
			err = fmt.Errorf("COSI systemd scope reconciliation failed: %s", status.Error)
		}
	}
	cancel()
	runErr := <-done
	if err != nil || runErr != nil {
		t.Fatalf("scope reconciliation: status=%v engine=%v", err, runErr)
	}
	return supervisor
}
