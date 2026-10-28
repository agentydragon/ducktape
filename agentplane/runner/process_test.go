package runner

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

const (
	processTestFixtureEnv = "AGENTPLANE_PROCESS_TEST_FIXTURE"
	processTestLockEnv    = "AGENTPLANE_PROCESS_TEST_LOCK"
	processTestPIDEnv     = "AGENTPLANE_PROCESS_TEST_PID"
	processTestDetached   = "--agentplane-process-test-detached"
	processTestLeader     = "--agentplane-process-test-leader"
)

func TestMain(m *testing.M) {
	if handled, code := InternalMode(os.Args[1:]); handled {
		os.Exit(code)
	}
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case processTestDetached:
			processTestDetachedMain(os.Args[2:])
			os.Exit(0)
		case processTestLeader:
			os.Exit(processTestLeaderMain(os.Args[2:]))
		}
	}
	os.Exit(m.Run())
}

func TestProcessParentDeathFencesNativeAndRetainsOwner(t *testing.T) {
	directory := t.TempDir()
	lockPath := filepath.Join(directory, "owner")
	pidPath := filepath.Join(directory, "native-pid")
	fixture := startProcessFixture(t, "parent-death", lockPath, pidPath)
	nativePID := waitForPIDFile(t, pidPath)
	probe := openLockProbe(t, lockPath)
	defer probe.Close()
	if !ownerLockHeld(probe) {
		t.Fatal("state owner lock was not retained by the running native process")
	}
	if err := fixture.Process.Kill(); err != nil {
		t.Fatalf("kill runner fixture: %v", err)
	}
	_ = fixture.Wait()
	// The supervisor grants the native process its persistence window while retaining the lock.
	time.Sleep(150 * time.Millisecond)
	if !ownerLockHeld(probe) {
		t.Fatal("state owner lock was released before the native process was fenced")
	}
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		if !pidExists(nativePID) && acquireOwnerLock(probe) {
			_ = unix.Flock(int(probe.Fd()), unix.LOCK_UN)
			return
		}
		time.Sleep(40 * time.Millisecond)
	}
	_ = unix.Kill(-nativePID, unix.SIGKILL)
	t.Fatalf("native process %d survived runner death or held ownership past the cleanup deadline", nativePID)
}

func TestBackgroundChildKeepsStateOwnerAfterNativeLeaderExits(t *testing.T) {
	directory := t.TempDir()
	lockPath := filepath.Join(directory, "owner")
	pidPath := filepath.Join(directory, "detached-pid")
	fixture := startProcessFixture(t, "background-child", lockPath, pidPath)
	if err := fixture.Wait(); err != nil {
		t.Fatalf("wait for runner fixture: %v; output=%q", err, fixture.output.String())
	}
	detachedPID := waitForPIDFile(t, pidPath)
	probe := openLockProbe(t, lockPath)
	defer probe.Close()
	if !ownerLockHeld(probe) {
		t.Fatal("detached child did not retain the state owner lock after its native leader exited")
	}
	if err := unix.Kill(detachedPID, unix.SIGKILL); err != nil && err != unix.ESRCH {
		t.Fatalf("kill detached child %d: %v", detachedPID, err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if acquireOwnerLock(probe) {
			_ = unix.Flock(int(probe.Fd()), unix.LOCK_UN)
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	t.Fatal("state owner lock remained held after the detached child exited")
}

func TestProcessFixture(t *testing.T) {
	switch os.Getenv(processTestFixtureEnv) {
	case "parent-death":
		testProcessParentDeathFixture(t)
	case "background-child":
		testBackgroundChildFixture(t)
	}
}

func TestResolveWorkspaceRejectsSymlinkEscape(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(root, "escape")); err != nil {
		t.Fatal(err)
	}
	if _, err := ResolveWorkspace(filepath.Join(root, "escape", "new"), root); err == nil || !strings.Contains(err.Error(), "must be a child") {
		t.Fatalf("ResolveWorkspace error = %v, want child-containment error", err)
	}
}

func TestProcessStartFailureCompletesWait(t *testing.T) {
	process, err := NewProcess(ProcessConfig{
		Command:              []string{"/bin/true"},
		supervisorExecutable: filepath.Join(t.TempDir(), "missing-runner"),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(context.Background()); err == nil {
		t.Fatal("Start succeeded with a missing runner executable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if code, err := process.Wait(ctx); code != 125 || err == nil {
		t.Fatalf("Wait after failed Start = (%d, %v), want (125, error)", code, err)
	}
}

func TestInputCancellationAndCloseUnblockPendingWrites(t *testing.T) {
	process, err := NewProcess(ProcessConfig{Command: []string{"/bin/sleep", "60"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	cleanupCtx, cancelCleanup := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancelCleanup()
	defer func() {
		if pid := process.PID(); pid != 0 {
			_ = unix.Kill(-pid, unix.SIGKILL)
		}
		_, _ = process.Wait(cleanupCtx)
	}()
	writeDone := make(chan error, 1)
	writeCtx, cancelWrite := context.WithCancel(context.Background())
	defer cancelWrite()
	go func() {
		writeDone <- process.Write(writeCtx, make([]byte, 8*1024*1024))
	}()
	// The harness never reads stdin, so this write must block once the pipe fills.
	time.Sleep(50 * time.Millisecond)
	cancelWrite()
	select {
	case err := <-writeDone:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("pending write after cancellation = %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("pending input write did not observe context cancellation")
	}
	go func() {
		writeDone <- process.Write(context.Background(), make([]byte, 8*1024*1024))
	}()
	select {
	case err := <-writeDone:
		t.Fatalf("previous cancellation prevented a new write: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	closed := make(chan error, 1)
	go func() { closed <- process.CloseStdin() }()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatalf("CloseStdin: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("CloseStdin was blocked behind a stalled input write")
	}
	select {
	case err := <-writeDone:
		if err == nil {
			t.Fatal("pending write unexpectedly succeeded after stdin was closed")
		}
	case <-time.After(time.Second):
		t.Fatal("pending input write did not observe stdin closure")
	}
	if err := process.Stop(context.Background(), 0); err != nil {
		t.Fatalf("Stop: %v", err)
	}
}

func TestImmediateStopFencesChildAndReleasesOwner(t *testing.T) {
	lockPath := filepath.Join(t.TempDir(), "owner")
	probe := openLockProbe(t, lockPath)
	defer probe.Close()
	for attempt := 0; attempt < 32; attempt++ {
		owner, err := os.OpenFile(lockPath, os.O_RDONLY|os.O_CREATE, 0o600)
		if err != nil {
			t.Fatal(err)
		}
		if err := unix.Flock(int(owner.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
			_ = owner.Close()
			t.Fatalf("acquire owner lock on attempt %d: %v", attempt, err)
		}
		process, err := NewProcess(ProcessConfig{
			Command:      []string{"/bin/sleep", "60"},
			StateOwnerFD: owner,
		})
		if err != nil {
			_ = owner.Close()
			t.Fatal(err)
		}
		if err := process.Start(context.Background()); err != nil {
			_ = owner.Close()
			t.Fatalf("start on attempt %d: %v", attempt, err)
		}
		pid := process.PID()
		_ = owner.Close()
		if acquireOwnerLock(probe) {
			_ = unix.Flock(int(probe.Fd()), unix.LOCK_UN)
			_ = unix.Kill(-pid, unix.SIGKILL)
			_, _ = process.Wait(context.Background())
			t.Fatalf("native process did not inherit state ownership on attempt %d", attempt)
		}
		if err := process.Stop(context.Background(), 0); err != nil {
			t.Fatalf("stop on attempt %d: %v", attempt, err)
		}
		if !acquireOwnerLock(probe) {
			_ = unix.Kill(-pid, unix.SIGKILL)
			t.Fatalf("attempt %d left native work alive or retained owner lock after Stop", attempt)
		}
		_ = unix.Flock(int(probe.Fd()), unix.LOCK_UN)
	}
}

func testProcessParentDeathFixture(t *testing.T) {
	owner := acquireStateOwnerForFixture(t)
	ignoringTerm := os.Getenv(processTestPIDEnv) + ".ignoring-term"
	process, err := NewProcess(ProcessConfig{
		Command:      []string{"/bin/sh", "-c", `trap '' TERM; : > "$1"; exec /bin/sleep 60`, "native", ignoringTerm},
		StateOwnerFD: owner,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := owner.Close(); err != nil {
		t.Fatal(err)
	}
	// Start reports the PID before the shell runs its trap, and a TERM before that kills it.
	for deadline := time.Now().Add(8 * time.Second); ; time.Sleep(10 * time.Millisecond) {
		if _, err := os.Stat(ignoringTerm); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("native process did not start ignoring TERM")
		}
	}
	if err := os.WriteFile(os.Getenv(processTestPIDEnv), []byte(strconv.Itoa(process.PID())), 0o600); err != nil {
		t.Fatal(err)
	}
	select {}
}

func testBackgroundChildFixture(t *testing.T) {
	owner := acquireStateOwnerForFixture(t)
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	process, err := NewProcess(ProcessConfig{
		Command:      []string{executable, processTestLeader, os.Getenv(processTestPIDEnv)},
		StateOwnerFD: owner,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := owner.Close(); err != nil {
		t.Fatal(err)
	}
	if err := process.CloseStdin(); err != nil {
		t.Fatal(err)
	}
	code, err := process.Wait(context.Background())
	if err != nil || code != 0 {
		t.Fatalf("wait for native leader: code=%d err=%v; %s", code, err, process.ExitDescription())
	}
}

func acquireStateOwnerForFixture(t *testing.T) *os.File {
	t.Helper()
	file, err := os.OpenFile(os.Getenv(processTestLockEnv), os.O_RDONLY|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	if err := unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		_ = file.Close()
		t.Fatal(err)
	}
	return file
}

func processTestLeaderMain(args []string) int {
	if len(args) != 1 {
		fmt.Fprintln(os.Stderr, "process test leader requires a pid file")
		return 64
	}
	executable, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	child := exec.Command(executable, processTestDetached, args[0])
	child.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	child.Stdin, child.Stdout, child.Stderr = os.Stdin, os.Stdout, os.Stderr
	stateOwner := os.NewFile(3, "test-state-owner")
	if stateOwner == nil {
		fmt.Fprintln(os.Stderr, "state owner descriptor is unavailable")
		return 1
	}
	child.ExtraFiles = []*os.File{stateOwner}
	if err := child.Start(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(args[0]); err == nil {
			return 0
		}
		time.Sleep(10 * time.Millisecond)
	}
	_ = child.Process.Kill()
	return 1
}

func processTestDetachedMain(args []string) {
	if len(args) != 1 {
		return
	}
	signal.Ignore(syscall.SIGTERM)
	_ = os.WriteFile(args[0], []byte(strconv.Itoa(os.Getpid())), 0o600)
	for {
		time.Sleep(time.Hour)
	}
}

type processFixture struct {
	*exec.Cmd
	output *safeBuffer
}

func startProcessFixture(t *testing.T, mode, lockPath, pidPath string) *processFixture {
	t.Helper()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(executable, "-test.run=^TestProcessFixture$")
	command.Env = append(
		os.Environ(),
		processTestFixtureEnv+"="+mode,
		processTestLockEnv+"="+lockPath,
		processTestPIDEnv+"="+pidPath,
	)
	output := &safeBuffer{}
	command.Stdout, command.Stderr = output, output
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	return &processFixture{Cmd: command, output: output}
}

type safeBuffer struct {
	mu   sync.Mutex
	data []byte
}

func (b *safeBuffer) Write(data []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.data = append(b.data, data...)
	return len(data), nil
}

func (b *safeBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return string(b.data)
}

func waitForPIDFile(t *testing.T, path string) int {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(path)
		if err == nil {
			pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
			if err == nil && pid > 0 {
				return pid
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for PID file %s", path)
	return 0
}

func openLockProbe(t *testing.T, path string) *os.File {
	t.Helper()
	file, err := os.OpenFile(path, os.O_RDONLY|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	return file
}

func ownerLockHeld(file *os.File) bool {
	err := unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB)
	if err == nil {
		_ = unix.Flock(int(file.Fd()), unix.LOCK_UN)
		return false
	}
	return errors.Is(err, unix.EWOULDBLOCK) || errors.Is(err, unix.EAGAIN)
}

func acquireOwnerLock(file *os.File) bool {
	return unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB) == nil
}

func pidExists(pid int) bool {
	err := unix.Kill(pid, 0)
	return err == nil || err == unix.EPERM
}

func TestPIDReportCancellationAndFraming(t *testing.T) {
	t.Run("split report", func(t *testing.T) {
		reader, writer, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		defer reader.Close()
		go func() {
			defer writer.Close()
			_, _ = writer.WriteString("12")
			_, _ = writer.WriteString("34\n")
		}()
		if pid, err := readPIDReport(context.Background(), reader); err != nil || pid != 1234 {
			t.Fatalf("PID report = %d, %v", pid, err)
		}
	})
	for _, report := range []string{"", "0\n", "not-a-pid\n", strings.Repeat("1", 33)} {
		t.Run(fmt.Sprintf("invalid %q", report), func(t *testing.T) {
			reader, writer, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			defer reader.Close()
			_, _ = writer.WriteString(report)
			_ = writer.Close()
			if pid, err := readPIDReport(context.Background(), reader); err == nil {
				t.Fatalf("invalid report accepted as %d", pid)
			}
		})
	}
	t.Run("cancel while writer stays open", func(t *testing.T) {
		reader, writer, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		defer reader.Close()
		defer writer.Close()
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		done := make(chan error, 1)
		go func() { _, err := readPIDReport(ctx, reader); done <- err }()
		cancel()
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("cancelled PID report = %v", err)
			}
		case <-time.After(time.Second):
			t.Fatal("PID read did not observe cancellation")
		}
	})
}
