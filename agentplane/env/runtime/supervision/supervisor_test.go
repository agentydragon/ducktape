package supervision

import (
	"bufio"
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestSupervisorFencesSameGroupChildAfterNativeLeaderExits(t *testing.T) {
	ownerPath := filepath.Join(t.TempDir(), "owner")
	owner, err := os.OpenFile(ownerPath, os.O_RDONLY|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if err := unix.Flock(int(owner.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		_ = owner.Close()
		t.Fatal(err)
	}
	pidFile := filepath.Join(t.TempDir(), "background-pid")
	childScript := "trap '' TERM; echo $$ > " + quoteShellArg(pidFile) + "; exec /bin/sleep 60"
	leaderScript := "/bin/sh -c " + quoteShellArg(childScript) + " & wait"
	process, err := NewSubprocessSupervisor(owner).NewProcess(ProcessConfig{
		Command:   []string{"/bin/sh", "-c", leaderScript},
		StopGrace: stopGrace,
	})
	if err != nil {
		_ = owner.Close()
		t.Fatal(err)
	}
	if err := process.Start(context.Background()); err != nil {
		_ = owner.Close()
		t.Fatal(err)
	}
	cleanupCtx, cancelCleanup := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancelCleanup()
	defer func() {
		if pid := process.PID(); pid != 0 {
			_ = unix.Kill(-pid, unix.SIGKILL)
		}
		if childPID := readPIDFile(pidFile); childPID > 0 {
			_ = unix.Kill(childPID, unix.SIGKILL)
		}
		_, _ = process.Wait(cleanupCtx)
	}()
	childPID := waitForProcessPID(t, pidFile)
	_ = owner.Close()
	probe, err := os.OpenFile(ownerPath, os.O_RDONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer probe.Close()
	if acquireOwnerLock(probe) {
		_ = unix.Flock(int(probe.Fd()), unix.LOCK_UN)
		t.Fatal("native child failed to inherit the state-owner lock")
	}
	if err := unix.Kill(process.PID(), unix.SIGKILL); err != nil {
		t.Fatalf("kill native leader: %v", err)
	}
	// The supervisor allows its full five-second TERM grace period before killing descendants.
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	if code, err := process.Wait(ctx); err != nil || code != 128+int(syscall.SIGKILL) {
		t.Fatalf("wait after native leader exit = (%d, %v)", code, err)
	}
	if err := unix.Kill(-process.PID(), 0); err != unix.ESRCH {
		t.Fatalf("process group still exists after Wait completed: %v", err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if acquireOwnerLock(probe) {
			_ = unix.Flock(int(probe.Fd()), unix.LOCK_UN)
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	_ = unix.Kill(childPID, unix.SIGKILL)
	t.Fatalf("same-group tool %d kept state ownership after its native leader exited", childPID)
}

func TestSubprocessSupervisorLossDoesNotConfirmNativeFence(t *testing.T) {
	process, err := NewSubprocessSupervisor(nil).NewProcess(ProcessConfig{
		Command:   []string{"/bin/sh", "-c", "exec 3<&-; exec /bin/sleep 60"},
		StopGrace: stopGrace,
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := process.Start(ctx); err != nil {
		t.Fatal(err)
	}
	nativePID := process.PID()
	if nativePID <= 1 {
		t.Fatalf("invalid native PID %d", nativePID)
	}
	backend := process.backend.(*subprocessBackend)
	if err := backend.cmd.Process.Kill(); err != nil {
		t.Fatalf("kill trusted supervisor: %v", err)
	}
	defer func() {
		if pidExists(nativePID) {
			_ = unix.Kill(nativePID, unix.SIGKILL)
		}
	}()
	if err := process.Stop(ctx); err == nil {
		t.Fatal("Stop succeeded after the supervisor died without confirming cleanup")
	}
	code, err := process.Wait(ctx)
	if err == nil || code != 128+int(syscall.SIGKILL) {
		t.Fatalf("Wait after supervisor loss = (%d, %v), want SIGKILL exit with a failed fence", code, err)
	}
	if !pidExists(nativePID) {
		t.Fatalf("native process %d did not survive supervisor loss as required by this regression", nativePID)
	}
}

func TestWaitNativeGroupGoneWaitsForLeaderExit(t *testing.T) {
	child := exec.Command("/bin/sleep", "60")
	child.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = child.Process.Kill()
		_ = child.Wait()
	}()
	done := make(chan error, 1)
	go func() { done <- waitNativeGroupGone(child.Process.Pid) }()
	select {
	case err := <-done:
		t.Fatalf("group disappearance completed while its leader was alive: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	if err := child.Process.Kill(); err != nil {
		t.Fatalf("kill group leader: %v", err)
	}
	if err := child.Wait(); err == nil {
		t.Fatal("Wait unexpectedly succeeded after SIGKILL")
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("wait for group disappearance: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("group disappearance did not complete after reaping the leader")
	}
}

func TestChildSubreaperAdoptsAndReapsSameGroupDescendant(t *testing.T) {
	cmd := exec.Command(os.Args[0], processTestSubreaper, filepath.Join(t.TempDir(), "descendant.pid"))
	var output strings.Builder
	cmd.Stdout, cmd.Stderr = &output, &output
	if err := cmd.Run(); err != nil {
		t.Fatalf("subreaper process failed: %v\n%s", err, output.String())
	}
	if !strings.Contains(output.String(), "reparented from native") || !strings.Contains(output.String(), "and was reaped") {
		t.Fatalf("subreaper process did not prove adoption and reaping: %s", output.String())
	}
}

func TestSubprocessStopUsesLaunchGrace(t *testing.T) {
	grace := 250 * time.Millisecond
	process, err := NewSubprocessSupervisor(nil).NewProcess(ProcessConfig{
		Command:   []string{"/bin/sh", "-c", `trap '' TERM; echo ready; exec /bin/sleep 60`},
		StopGrace: grace,
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := process.Start(ctx); err != nil {
		t.Fatal(err)
	}
	if line, err := bufio.NewReader(process.Stdout()).ReadString('\n'); err != nil || line != "ready\n" {
		t.Fatalf("native readiness = %q, %v", line, err)
	}
	started := time.Now()
	if err := process.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if elapsed := time.Since(started); elapsed < grace || elapsed > 3*time.Second {
		t.Fatalf("Stop took %s with %s launch grace; want graceful wait through deadline before escalation", elapsed, grace)
	}
	if code, err := process.Wait(ctx); err != nil || code != 128+int(syscall.SIGKILL) {
		t.Fatalf("Wait after escalated stop = (%d, %v), want SIGKILL exit", code, err)
	}
}

func TestSubprocessCanceledStopRequestsSupervisorEscalation(t *testing.T) {
	process, err := NewSubprocessSupervisor(nil).NewProcess(ProcessConfig{
		Command:   []string{"/bin/sh", "-c", `trap '' TERM; echo ready; exec /bin/sleep 60`},
		StopGrace: 5 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	startCtx, cancelStart := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelStart()
	if err := process.Start(startCtx); err != nil {
		t.Fatal(err)
	}
	if line, err := bufio.NewReader(process.Stdout()).ReadString('\n'); err != nil || line != "ready\n" {
		t.Fatalf("native readiness = %q, %v", line, err)
	}
	stopCtx, cancelStop := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancelStop()
	started := time.Now()
	err = process.Stop(stopCtx)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Stop with expired context = %v, want deadline exceeded", err)
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("Stop returned after %s despite its 50ms deadline", elapsed)
	}
	waitCtx, cancelWait := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancelWait()
	if code, err := process.Wait(waitCtx); err != nil || code != 128+int(syscall.SIGKILL) {
		t.Fatalf("Wait after canceled stop = (%d, %v), want fenced SIGKILL exit", code, err)
	}
}

func quoteShellArg(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func waitForProcessPID(t *testing.T, path string) int {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if pid := readPIDFile(path); pid > 0 {
			return pid
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for child PID in %s", path)
	return 0
}

func readPIDFile(path string) int {
	data, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil || pid <= 0 {
		return 0
	}
	return pid
}
