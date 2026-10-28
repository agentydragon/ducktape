package runner

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
	"github.com/google/uuid"
)

func TestMain(m *testing.M) {
	if handled, code := InternalMode(os.Args[1:]); handled {
		os.Exit(code)
	}
	os.Exit(m.Run())
}

// This target runs inside the actual systemd VM guest, before the real Runner.
// It is deliberately excluded from runner_test: no fake manager establishes these claims.
func TestSystemdKernelFixture(t *testing.T) {
	t.Setenv("AGENTPLANE_PARENT_ONLY_SENTINEL", "must not reach native services")
	if err := os.Remove("/state/runner-systemd-proof.json"); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	config, _, _, err := LoadConfiguration("/run/agentplane/config/runner.json", "/run/agentplane/sandbox/launch.json")
	if err != nil {
		t.Fatal(err)
	}
	config.StateDir = "/state/.supervision-proof"
	policy := config.VMProcessPolicy
	if policy == nil {
		t.Fatal("systemd proof requires VM configuration")
	}
	if err := policy.prepare(config); err != nil {
		t.Fatal(err)
	}
	policy.startTransient = func(c context.Context, bus *systemd.Conn, name string, props []systemd.Property, completion chan<- string) (int, error) {
		job, err := bus.StartTransientUnitContext(c, name, "fail", props, completion)
		t.Logf("native launch enqueue: unit=%s job=%d error=%v kind=%s", name, job, err, systemdErrorKind(err))
		return job, err
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

	t.Run("workspace_created_as_agent", func(t *testing.T) {
		cwd := filepath.Join(dir, "nested", "workspace")
		created, err := EnsureWorkspace(ctx, cwd, dir, policy)
		if err != nil {
			t.Fatal(err)
		}
		if created != cwd {
			t.Fatalf("created=%s want %s", created, cwd)
		}
		p := startSystemdProof(t, ctx, policy, cwd, "test -w .; printf writable")
		stdout, stderr := readProofStreams(p)
		if code, err := p.Wait(ctx); err != nil || code != 0 {
			t.Fatalf("workspace access code=%d err=%v", code, err)
		}
		if text := <-stdout; text != "writable" {
			t.Fatalf("workspace proof=%s", text)
		}
		<-stderr
	})
	t.Run("streams_identity_and_resources", func(t *testing.T) {
		p := startSystemdProof(t, ctx, policy, dir, "printf 'out:%s:%s:%s\\n' \"$(id -u)\" \"$(id -g)\" \"$PWD\"; printf 'err:%s\\n' \"$SENTINEL\" >&2; printf 'home:%s\\n' \"$HOME\"; test -z \"${AGENTPLANE_PARENT_ONLY_SENTINEL+x}\"; IFS= read -r line; printf 'input:%s\\n' \"$line\"")
		stdout, stderr := readProofStreams(p)
		props, err := conn.GetUnitTypePropertiesContext(ctx, p.systemd.name, "Service")
		if err != nil {
			t.Fatal(err)
		}
		for name, want := range map[string]any{"MemoryMax": uint64(policy.ProcessLimits.MemoryMaxBytes), "MemorySwapMax": uint64(0), "TasksMax": uint64(policy.ProcessLimits.PidsMax), "CPUQuotaPerSecUSec": uint64(policy.ProcessLimits.CPUQuotaMicros) * 10, "OOMPolicy": "continue", "Slice": workloadSlice} {
			if props[name] != want {
				t.Fatalf("%s=%v want %v", name, props[name], want)
			}
		}

		group, ok := props["ControlGroup"].(string)
		if !ok || group == "" {
			t.Fatal("native service has no cgroup")
		}
		assertKernelBudget(t, filepath.Join("/sys/fs/cgroup", group), policy.ProcessLimits)
		file, err := readConfiguration("/run/agentplane/config/runner.json")
		if err != nil {
			t.Fatal(err)
		}
		_, _, vm, err := file.resolveEnvironment()
		if err != nil {
			t.Fatal(err)
		}
		assertKernelBudget(t, "/sys/fs/cgroup/agentplane.slice", ResourceLimits{CPUQuotaMicros: vm.Resources.ServiceCPUQuotaMicros, MemoryMaxBytes: vm.Resources.ServiceMemoryMaxBytes, PidsMax: vm.Resources.ServicePidsMax})
		high, err := os.ReadFile("/sys/fs/cgroup/agentplane.slice/memory.high")
		if err != nil || strings.TrimSpace(string(high)) != strconv.FormatInt(vm.Resources.ServiceMemoryHighBytes, 10) {
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
		assertSystemdProofEmpty(t, ctx, conn, p.systemd.name)
	})

	t.Run("argv_is_verbatim_and_only_shell_expands_environment", func(t *testing.T) {
		script := `printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "$4"; printf 'shell:%s:%s\n' "$HOME" "${SENTINEL}"`
		literals := []string{"$HOME", "${SENTINEL}", "%n", `single'quote"$`}
		command := append([]string{"/bin/sh", "-eu", "-c", script, "--"}, literals...)
		p, err := NewProcess(ProcessConfig{Command: command, Dir: dir, Env: map[string]string{"PATH": "/bin:/usr/bin", "HOME": "/explicit/proof-home", "SENTINEL": "shell sentinel"}, VMProcessPolicy: policy})
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
		p := startSystemdProof(t, ctx, policy, dir, "setsid /bin/sh -c 'trap \"\" TERM; while :; do sleep 1; done' & echo $!; exit 0")
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
		assertSystemdProofEmpty(t, ctx, conn, p.systemd.name)
	})

	t.Run("invalid_launch_cannot_escape_fence", func(t *testing.T) {
		p, err := NewProcess(ProcessConfig{Command: []string{"/missing-agentplane-proof-binary"}, Dir: dir, Env: map[string]string{}, VMProcessPolicy: policy})
		if err != nil {
			t.Fatal(err)
		}
		if err := p.Start(ctx); err == nil {
			t.Fatal("invalid native executable started")
		}
		if _, err := p.Wait(ctx); err == nil {
			t.Fatal("invalid start lost error")
		}
		if p.systemd == nil {
			t.Fatal("invalid launch never reached manager")
		}
		assertSystemdProofEmpty(t, ctx, conn, p.systemd.name)
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
			p, err := NewProcess(ProcessConfig{Command: []string{"/bin/sh", "-c", "sleep 60"}, Dir: dir, Env: map[string]string{"PATH": "/bin:/usr/bin"}, VMProcessPolicy: &owned})
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
			if _, err := conn.StartTransientUnitContext(ctx, name, "fail", proofUnitProperties("/bin/sleep", []string{"/bin/sleep", "60"}, workloadSlice), nil); err != nil {
				t.Fatal(err)
			}
			defer fenceSystemdUnit(context.Background(), conn, name, true)
		}
		recovered := *policy
		if err := recovered.prepare(config); err != nil {
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
		if _, err := conn.StartTransientUnitContext(ctx, parent, "fail", proofUnitProperties("/bin/sleep", []string{"/bin/sleep", "60"}, "agentplane.slice"), nil); err != nil {
			t.Fatal(err)
		}
		defer fenceSystemdUnit(context.Background(), conn, parent, true)
		owned := *policy
		owned.lifetimeUnit = parent
		marker := filepath.Join(dir, "saved")
		p := startSystemdProof(t, ctx, &owned, dir, "trap 'printf saved > "+marker+"; exit 0' TERM; printf ready; while :; do sleep 1; done")
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
		assertSystemdProofEmpty(t, ctx, conn, p.systemd.name)
	})

	t.Run("ordinary_stop_retains_graceful_exit_status", func(t *testing.T) {
		p := startSystemdProof(t, ctx, policy, dir, "trap 'exit 23' TERM; printf ready; while :; do sleep 1; done")
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
		assertSystemdProofEmpty(t, ctx, conn, p.systemd.name)
	})
	t.Run("ordinary_stop_waits_for_domain", func(t *testing.T) {
		p := startSystemdProof(t, ctx, policy, dir, "trap '' TERM; printf ready; sleep 60 & wait")
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
		assertSystemdProofEmpty(t, ctx, conn, p.systemd.name)
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

func startSystemdProof(t *testing.T, ctx context.Context, policy *VMProcessPolicy, dir, script string) *Process {
	t.Helper()
	p, err := NewProcess(ProcessConfig{Command: []string{"/bin/sh", "-eu", "-c", script}, Dir: dir, Env: map[string]string{"PATH": "/bin:/usr/bin", "HOME": "/explicit/proof-home", "SENTINEL": "proof sentinel"}, VMProcessPolicy: policy})
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

func assertKernelBudget(t *testing.T, group string, limits ResourceLimits) {
	t.Helper()
	for file, want := range map[string]string{"cpu.max": fmt.Sprintf("%d %d", limits.CPUQuotaMicros, cgroupCPUPeriodMicros), "memory.max": strconv.FormatInt(limits.MemoryMaxBytes, 10), "memory.swap.max": "0", "memory.oom.group": "0", "pids.max": strconv.FormatInt(limits.PidsMax, 10)} {
		data, err := os.ReadFile(filepath.Join(group, file))
		if err != nil {
			t.Fatal(err)
		}
		if got := strings.TrimSpace(string(data)); got != want {
			t.Fatalf("%s/%s=%q want %q", group, file, got, want)
		}
	}
}
