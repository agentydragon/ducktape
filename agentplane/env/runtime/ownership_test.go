package runtime

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harness"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
)

const ownershipFixtureMode = "--test-runtime-ownership-fixture"

// This child uses production Host construction. Its setup process must inherit
// Runtime's flock so a replacement stays excluded after the management process dies.
func runOwnershipFixture(args []string) error {
	root, endpoint, history := args[0], args[1], args[2]
	launch := configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}}
	config := Config{
		StateDir: root, Launch: launch, Supervision: "subprocess", Control: insecureControl(),
		HistoryIngress: configuration.Endpoint{Target: history},
		WorkspaceRoot:  root, InitializationCwd: root,
		Claude: &harness.LaunchConfig{Binary: "/bin/false", BaseURL: endpoint, Token: "test"},
	}
	r, err := New(config)
	if err != nil {
		return err
	}
	ctx := context.Background()
	if err := r.startSessions(); err != nil {
		return err
	}
	if err := r.waitForInitialSessions(ctx); err != nil {
		return err
	}
	if err := r.adoptIncarnation(ctx, "test-incarnation"); err != nil {
		return err
	}
	setup := fmt.Sprintf("trap '' TERM\nsleep 60 &\nprintf '%%s\\n' $! > %s\nprintf '%%s\\n' $$ > %s\nmv %s %s\nwait", shellQuote(filepath.Join(root, "descendant-pid")), shellQuote(filepath.Join(root, "native-pid.tmp")), shellQuote(filepath.Join(root, "native-pid.tmp")), shellQuote(filepath.Join(root, "native-pid")))
	request := openLifecycleSession("session-1", filepath.Join(root, "workspace"))
	request.SetupScript = &setup
	record, err := retained.NewRecord(request.Spec, setup, nil)
	if err != nil {
		return err
	}
	if _, err := r.sessions.ReserveFresh(ctx, request.SessionId, func() error { return r.store.Write(request.SessionId, record) }); err != nil {
		return err
	}
	select {}
}

func TestHostSubprocessRetainsRuntimeFenceAfterManagementProcessDeath(t *testing.T) {
	root := t.TempDir()
	proxy := modelConfigServer(t)
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	log, err := os.Create(filepath.Join(root, "fixture.log"))
	if err != nil {
		t.Fatal(err)
	}
	defer log.Close()
	child := exec.Command(executable, ownershipFixtureMode, root, proxy.URL, startAcknowledgingHistory(t))
	child.Stdout, child.Stderr = log, log
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	var nativePID, descendantPID, supervisorPID int
	t.Cleanup(func() {
		_ = child.Process.Kill()
		if child.ProcessState == nil {
			_ = child.Wait()
		}
		if supervisorPID != 0 {
			_ = syscall.Kill(supervisorPID, syscall.SIGCONT)
		}
		if descendantPID != 0 {
			_ = syscall.Kill(descendantPID, syscall.SIGKILL)
		}
		if nativePID != 0 {
			_ = syscall.Kill(-nativePID, syscall.SIGKILL)
		}
	})
	pidPath := filepath.Join(root, "native-pid")
	waitForFile(t, pidPath)
	data, err := os.ReadFile(pidPath)
	if err != nil {
		t.Fatal(err)
	}
	nativePID, err = strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		t.Fatal(err)
	}
	data, err = os.ReadFile(filepath.Join(root, "descendant-pid"))
	if err != nil {
		t.Fatal(err)
	}
	descendantPID, err = strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		t.Fatal(err)
	}
	nativeOwner, err := os.Stat(fmt.Sprintf("/proc/%d/fd/3", nativePID))
	if err != nil {
		t.Fatalf("native process has no inherited owner descriptor: %v", err)
	}
	runtimeOwner, err := os.Stat(filepath.Join(root, environmentRuntimeOwnerName))
	if err != nil || !os.SameFile(runtimeOwner, nativeOwner) {
		t.Fatalf("native process did not inherit the Runtime owner inode: %v", err)
	}
	data, err = os.ReadFile(fmt.Sprintf("/proc/%d/status", nativePID))
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "PPid:") {
			supervisorPID, err = strconv.Atoi(strings.TrimSpace(strings.TrimPrefix(line, "PPid:")))
			if err != nil {
				t.Fatal(err)
			}
		}
	}
	if supervisorPID <= 1 || supervisorPID == child.Process.Pid {
		t.Fatalf("native parent %d is not a subprocess supervisor", supervisorPID)
	}
	// Freeze cleanup before killing Runtime so the fence can be inspected at
	// a deterministic point while the native process and supervisor survive.
	if err := syscall.Kill(supervisorPID, syscall.SIGSTOP); err != nil {
		t.Fatal(err)
	}
	waitForSupervisorStop(t, supervisorPID)
	if err := child.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	if err := child.Wait(); err == nil {
		t.Fatal("killed Runtime exited successfully")
	}
	if err := syscall.Kill(nativePID, 0); err != nil {
		t.Fatalf("native process did not survive Runtime death until its supervisor fences it: %v", err)
	}
	if err := syscall.Kill(descendantPID, 0); err != nil {
		t.Fatalf("native descendant did not survive Runtime death: %v", err)
	}
	if owner, err := openEnvironmentOwner(root); err == nil {
		_ = owner.Close()
		t.Fatal("replacement acquired Runtime ownership while the native process survived")
	}
	if err := syscall.Kill(supervisorPID, syscall.SIGCONT); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		if owner, err := openEnvironmentOwner(root); err == nil {
			_ = owner.Close()
			for _, pid := range []int{nativePID, descendantPID} {
				data, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
				if os.IsNotExist(err) {
					continue
				}
				// A dead orphan may await reaping by the fixture container's PID1.
				if err != nil || !strings.Contains(string(data), "\nState:\tZ") {
					t.Fatalf("replacement acquired ownership before process %d died: %s, %v", pid, data, err)
				}
			}
			return
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			data, _ := os.ReadFile(log.Name())
			t.Fatalf("supervisor did not fence native descendants and release ownership: %s", data)
		}
	}
}

func waitForSupervisorStop(t *testing.T, pid int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		data, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(data), "\nState:\tT") {
			return
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			t.Fatal("supervisor did not stop before Runtime death")
		}
	}
}
