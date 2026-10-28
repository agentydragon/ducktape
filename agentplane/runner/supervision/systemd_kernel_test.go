package supervision

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/godbus/dbus/v5"
	"github.com/google/uuid"
)

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && os.Args[1] == SupervisorMode {
		os.Exit(SupervisorMain(os.Args[2:]))
	}
	os.Exit(m.Run())
}

// This target runs inside the actual systemd VM guest, before the real Runner.
// It is deliberately excluded from supervision_test: no fake manager establishes these claims.
func TestSystemdKernelFixture(t *testing.T) {
	t.Setenv("AGENTPLANE_PARENT_ONLY_SENTINEL", "must not reach native services")
	if err := os.Remove("/state/runner-systemd-proof.json"); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	config, sandboxUID := loadSystemdProofConfiguration(t)
	if config.Environment.Kind != "vm" {
		t.Fatal("systemd proof requires VM configuration")
	}
	policy := &SystemdPolicy{
		Slice: WorkloadSlice, AgentUID: config.Environment.AgentUID, AgentGID: config.Environment.AgentGID,
		BlockDevices: config.Environment.BlockDevices, AggregateLimits: config.Environment.Resources.Aggregate,
		ProcessLimits: config.Environment.Resources.Process,
	}
	policy.startTransient = func(c context.Context, bus *systemd.Conn, name string, props []systemd.Property, completion chan<- string) (int, error) {
		job, err := bus.StartTransientUnitContext(c, name, "fail", props, completion)
		t.Logf("native launch enqueue: unit=%s job=%d error=%v kind=%s", name, job, err, systemdErrorKind(err))
		return job, err
	}
	stateDir := "/state/.supervision-proof"
	supervisor, err := policy.Prepare(stateDir, sandboxUID)
	if err != nil {
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
		stale := *policy
		stale.lifetimeMarker = filepath.Join(runnerLifetimeDirectory, strings.ReplaceAll(uuid.NewString(), "-", ""))
		effect := filepath.Join(dir, "stale-runner-effect")
		p, err := stale.newSupervisor().NewProcess(ProcessConfig{Command: []string{"/bin/busybox", "touch", effect}, Dir: dir, Env: map[string]string{}})
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

	t.Run("workspace_created_as_agent", func(t *testing.T) {
		cwd := filepath.Join(dir, "nested", "workspace")
		workspaceHelper, err := supervisor.NewProcess(ProcessConfig{
			// Keep this private Runner CLI mode in sync with workspace.HelperMode.
			Command: []string{"/run/agentplane/runner/bin/agentplane-runner", "--agentplane-runner-workspace", dir, "nested", "workspace"},
			Dir:     dir,
			Env:     map[string]string{},
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := workspaceHelper.Start(ctx); err != nil {
			t.Fatal(err)
		}
		workspaceStdout, workspaceStderr := readProofStreams(workspaceHelper)
		if err := workspaceHelper.CloseStdin(); err != nil {
			t.Fatal(err)
		}
		workspaceCode, workspaceErr := workspaceHelper.Wait(ctx)
		workspaceOutput, workspaceError := <-workspaceStdout, <-workspaceStderr
		if workspaceErr != nil || workspaceCode != 0 {
			t.Fatalf("workspace helper code=%d err=%v stdout=%q stderr=%q", workspaceCode, workspaceErr, workspaceOutput, workspaceError)
		}

		p := startSystemdProof(t, ctx, supervisor, cwd, "test -w .; printf writable")
		stdout, stderr := readProofStreams(p)
		if code, err := p.Wait(ctx); err != nil || code != 0 {
			t.Fatalf("workspace access code=%d err=%v", code, err)
		}
		if text := <-stdout; text != "writable" {
			t.Fatalf("workspace proof=%s", text)
		}
		<-stderr
		for _, path := range []string{filepath.Join(dir, "nested"), cwd} {
			info, err := os.Stat(path)
			if err != nil {
				t.Fatal(err)
			}
			owner, ok := info.Sys().(*syscall.Stat_t)
			if !ok || owner.Uid != policy.AgentUID || owner.Gid != policy.AgentGID {
				t.Fatalf("workspace directory %s owner=(%v,%v), want uid=%d gid=%d", path, owner, ok, policy.AgentUID, policy.AgentGID)
			}
		}
	})
	t.Run("streams_identity_and_resources", func(t *testing.T) {
		p := startSystemdProof(t, ctx, supervisor, dir, "printf 'out:%s:%s:%s\\n' \"$(id -u)\" \"$(id -g)\" \"$PWD\"; printf 'err:%s\\n' \"$SENTINEL\" >&2; printf 'home:%s\\n' \"$HOME\"; test -z \"${AGENTPLANE_PARENT_ONLY_SENTINEL+x}\"; IFS= read -r line; printf 'input:%s\\n' \"$line\"")
		stdout, stderr := readProofStreams(p)
		unitName := systemdProofUnitName(t, p)
		props, err := conn.GetUnitTypePropertiesContext(ctx, unitName, "Service")
		if err != nil {
			t.Fatal(err)
		}
		for name, want := range map[string]any{"MemoryMax": uint64(policy.ProcessLimits.MemoryMaxBytes), "MemorySwapMax": uint64(0), "TasksMax": uint64(policy.ProcessLimits.PidsMax), "CPUQuotaPerSecUSec": uint64(policy.ProcessLimits.CPUQuotaMicros) * 10, "OOMPolicy": "continue", "Slice": WorkloadSlice} {
			if props[name] != want {
				t.Fatalf("%s=%v want %v", name, props[name], want)
			}
		}

		group, ok := props["ControlGroup"].(string)
		if !ok || group == "" {
			t.Fatal("native service has no cgroup")
		}
		assertKernelBudget(t, filepath.Join("/sys/fs/cgroup", group), policy.ProcessLimits)
		resources := config.Environment.Resources
		assertKernelBudget(t, "/sys/fs/cgroup/agentplane.slice", ResourceLimits{CPUQuotaMicros: resources.ServiceCPUQuotaMicros, MemoryMaxBytes: resources.ServiceMemoryMaxBytes, PidsMax: resources.ServicePidsMax})
		high, err := os.ReadFile("/sys/fs/cgroup/agentplane.slice/memory.high")
		if err != nil || strings.TrimSpace(string(high)) != strconv.FormatInt(resources.ServiceMemoryHighBytes, 10) {
			t.Fatalf("parent memory.high=%q err=%v", high, err)
		}
		nativeStatus, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", p.PID()))
		if err != nil {
			t.Fatal(err)
		}
		statusFields := map[string][]string{}
		for _, line := range strings.Split(string(nativeStatus), "\n") {
			name, value, found := strings.Cut(line, ":")
			if found {
				statusFields[name] = strings.Fields(value)
			}
		}
		for name, want := range map[string]string{"NoNewPrivs": "1", "CapEff": "0000000000000000", "CapPrm": "0000000000000000", "CapAmb": "0000000000000000", "CapBnd": "0000000000000000"} {
			if got := strings.Join(statusFields[name], " "); got != want {
				t.Fatalf("native %s=%q want %q", name, got, want)
			}
		}
		if err := p.WriteLine(ctx, "verbatim input"); err != nil {
			t.Fatal(err)
		}
		code, err := p.Wait(ctx)
		if err != nil || code != 0 {
			t.Fatalf("wait code=%d err=%v", code, err)
		}
		want := fmt.Sprintf("out:%d:%d:%s\nhome:/explicit/proof-home\ninput:verbatim input\n", policy.AgentUID, policy.AgentGID, dir)
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
		p, err := supervisor.NewProcess(ProcessConfig{Command: command, Dir: dir, Env: map[string]string{"PATH": "/bin:/usr/bin", "HOME": "/explicit/proof-home", "SENTINEL": "shell sentinel"}})
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
	t.Run("leader_exit_fences_detached_descendants", func(t *testing.T) {
		p := startSystemdProof(t, ctx, supervisor, dir, "setsid /bin/sh -c 'trap \"\" TERM; while :; do sleep 1; done' & echo $!; exit 0")
		stdout, stderr := readProofStreams(p)
		if code, err := p.Wait(ctx); err != nil || code != 0 {
			t.Fatalf("wait code=%d err=%v", code, err)
		}
		text := strings.TrimSpace(<-stdout)
		<-stderr
		pid, err := strconv.Atoi(text)
		if err != nil {
			t.Fatalf("child PID %q: %v", text, err)
		}
		if data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid)); err == nil && !strings.Contains(string(data), ") Z ") {
			t.Fatalf("detached child survived: %s", data)
		}
		assertSystemdProofEmpty(t, ctx, conn, systemdProofUnitName(t, p))
	})

	t.Run("invalid_launch_cannot_escape_fence", func(t *testing.T) {
		p, err := supervisor.NewProcess(ProcessConfig{Command: []string{"/missing-agentplane-proof-binary"}, Dir: dir, Env: map[string]string{}})
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
			owned := *policy
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
			p, err := owned.newSupervisor().NewProcess(ProcessConfig{Command: []string{"/bin/sh", "-c", "sleep 60"}, Dir: dir, Env: map[string]string{"PATH": "/bin:/usr/bin"}})
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
		ownedName := policy.scope + uuid.NewString() + ".service"
		unrelatedName := "agentplane-proof-unrelated-" + uuid.NewString() + ".service"
		for _, name := range []string{ownedName, unrelatedName} {
			if _, err := conn.StartTransientUnitContext(ctx, name, "fail", proofUnitProperties("/bin/busybox", []string{"/bin/busybox", "sleep", "60"}, WorkloadSlice), nil); err != nil {
				t.Fatal(err)
			}
			defer fenceSystemdUnit(context.Background(), conn, name, true, "")
		}
		recovered := *policy
		if _, err := recovered.Prepare(stateDir, sandboxUID); err != nil {
			t.Fatal(err)
		}
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
		defer fenceSystemdUnit(context.Background(), conn, parent, true, "")
		owned := *policy
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
		if err := p.Stop(ctx, stopGrace); err != nil {
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
		p := startSystemdProof(t, ctx, supervisor, dir, "trap '' TERM; printf ready; sleep 60 & wait")
		ready := make([]byte, 5)
		if _, err := io.ReadFull(p.Stdout(), ready); err != nil {
			t.Fatal(err)
		}
		stdout, stderr := readProofStreams(p)
		if err := p.Stop(ctx, 50*time.Millisecond); err != nil {
			t.Fatal(err)
		}
		if code, err := p.Wait(ctx); err != nil || code != 137 {
			t.Fatalf("forcedstop exit=%d error=%v want137", code, err)
		}
		<-stdout
		<-stderr
		assertSystemdProofEmpty(t, ctx, conn, systemdProofUnitName(t, p))
	})
	t.Run("reap_retries_property_deadline_on_connected_manager", func(t *testing.T) {
		owned := *policy
		authoritativePID := make(chan uint32, 1)
		propertyReads := 0 // Only this process's reaper calls the hook.
		owned.reapServiceProperties = func(readCtx context.Context, bus *systemd.Conn, name string) (map[string]any, error) {
			propertyReads++
			if propertyReads == 1 {
				return nil, context.DeadlineExceeded
			}
			props, err := bus.GetUnitTypePropertiesContext(readCtx, name, "Service")
			if err == nil {
				if mainPID, ok := props["MainPID"].(uint32); ok {
					select {
					case authoritativePID <- mainPID:
					default:
					}
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
	if err := os.WriteFile("/state/runner-systemd-proof.json", []byte("{\"real_systemd_supervision\":\"passed\"}\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod("/state/runner-systemd-proof.json", 0644); err != nil {
		t.Fatal(err)
	}
}

func startSystemdProof(t *testing.T, ctx context.Context, supervisor *Supervisor, dir, script string) *Process {
	t.Helper()
	p, err := supervisor.NewProcess(ProcessConfig{Command: []string{"/bin/sh", "-eu", "-c", script}, Dir: dir, Env: map[string]string{"PATH": "/bin:/usr/bin", "HOME": "/explicit/proof-home", "SENTINEL": "proof sentinel"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Start(ctx); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if p.Running() {
			_ = p.Stop(context.Background(), stopGrace)
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

func assertSystemdProofEmpty(t *testing.T, ctx context.Context, conn *systemd.Conn, name string) {
	t.Helper()
	props, err := conn.GetUnitPropertiesContext(ctx, name)
	if systemdMissing(err) {
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	if state := props["ActiveState"]; state != "inactive" && state != "failed" {
		t.Fatalf("unit %s still %v", name, state)
	}
	service, err := conn.GetUnitTypePropertiesContext(ctx, name, "Service")
	if systemdMissing(err) {
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	group, _ := service["ControlGroup"].(string)
	if empty, err := systemdGroupEmpty(group); err != nil || !empty {
		t.Fatalf("unit %s kernel domain still populated: empty=%v err=%v", name, empty, err)
	}
}

func proofUnitProperties(executable string, args []string, slice string) []systemd.Property {
	return []systemd.Property{property("Type", "exec"), property("ExecStartEx", []systemdExec{{executable, args, []string{"no-env-expand"}}}), property("Slice", slice), property("RemainAfterExit", true), property("CollectMode", "inactive"), property("KillMode", "control-group"), property("TimeoutStopUSec", uint64(stopGrace/time.Microsecond))}
}

type systemdProofRunnerConfig struct {
	StateDir    string `json:"state_dir"`
	Environment struct {
		Kind         string   `json:"kind"`
		AgentUID     uint32   `json:"agent_uid"`
		AgentGID     uint32   `json:"agent_gid"`
		BlockDevices []string `json:"block_devices"`
		Resources    struct {
			ServiceMemoryHighBytes int64          `json:"service_memory_high_bytes"`
			ServiceMemoryMaxBytes  int64          `json:"service_memory_max_bytes"`
			ServiceCPUQuotaMicros  int64          `json:"service_cpu_quota_micros"`
			ServicePidsMax         int64          `json:"service_pids_max"`
			Aggregate              ResourceLimits `json:"aggregate"`
			Process                ResourceLimits `json:"process"`
		} `json:"resources"`
	} `json:"environment"`
}

func loadSystemdProofConfiguration(t *testing.T) (systemdProofRunnerConfig, string) {
	t.Helper()
	var config systemdProofRunnerConfig
	readSystemdProofJSON(t, "/run/agentplane/config/runner.json", &config)
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

func assertKernelBudget(t *testing.T, group string, limits ResourceLimits) {
	t.Helper()
	for file, want := range map[string]string{"cpu.max": fmt.Sprintf("%d %d", limits.CPUQuotaMicros, CPUPeriodMicros), "memory.max": strconv.FormatInt(limits.MemoryMaxBytes, 10), "memory.swap.max": "0", "memory.oom.group": "0", "pids.max": strconv.FormatInt(limits.PidsMax, 10)} {
		data, err := os.ReadFile(filepath.Join(group, file))
		if err != nil {
			t.Fatal(err)
		}
		if got := strings.TrimSpace(string(data)); got != want {
			t.Fatalf("%s/%s=%q want %q", group, file, got, want)
		}
	}
}
