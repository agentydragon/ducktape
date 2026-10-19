package runner

import (
	"bufio"
	"context"
	"fmt"
	"os"
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
	childScript := "trap '' TERM; echo $$ > " + shellQuote(pidFile) + "; exec /bin/sleep 60"
	leaderScript := "/bin/sh -c " + shellQuote(childScript) + " & wait"
	process, err := NewProcess(ProcessConfig{
		Command:      []string{"/bin/sh", "-c", leaderScript},
		StateOwnerFD: owner,
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

func TestSupervisorDropsAgentIdentityAndStopsNativeProcess(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Fatal("cross-UID supervisor coverage requires a privileged Linux test worker")
	}
	uid, gid := uint32(65534), uint32(65534)
	process, err := NewProcess(ProcessConfig{
		Command:  []string{"/bin/sh", "-c", `printf '%s:%s\n' "$(id -u)" "$(id -g)"; exec /bin/sleep 60`},
		Env:      map[string]string{"PATH": "/usr/bin:/bin"},
		AgentUID: &uid,
		AgentGID: &gid,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	line, err := bufio.NewReader(process.Stdout()).ReadString('\n')
	if err != nil {
		_ = process.Stop(context.Background(), 0)
		t.Fatalf("read agent identity: %v", err)
	}
	if got, want := strings.TrimSpace(line), fmt.Sprintf("%d:%d", uid, gid); got != want {
		_ = process.Stop(context.Background(), 0)
		t.Fatalf("native effective identity = %q, want %q", got, want)
	}
	if err := process.Stop(context.Background(), 0); err != nil {
		t.Fatalf("Stop cross-UID native process: %v", err)
	}
	code, err := process.Wait(context.Background())
	if err != nil || (code != 128+int(syscall.SIGTERM) && code != 128+int(syscall.SIGKILL)) {
		t.Fatalf("cross-UID native process exit = (%d, %v), want SIGTERM or SIGKILL", code, err)
	}
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
