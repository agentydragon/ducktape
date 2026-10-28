package supervision

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// This binary runs as container PID 1, outside supervision_test's process tree.
func TestMain(m *testing.M) {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case SupervisorMode:
			os.Exit(SupervisorMain(os.Args[2:]))
		case "--pid1-proof-native":
			os.Exit(pid1ProofNative(os.Args[2]))
		case "--pid1-proof-fast":
			fmt.Fprint(os.Stdout, "stdout")
			fmt.Fprint(os.Stderr, "stderr")
			os.Exit(42)
		case "--pid1-proof-orphan":
			child := exec.Command(os.Args[0], "--pid1-proof-fast")
			child.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
			if err := child.Start(); err != nil {
				panic(err)
			}
			fmt.Fprint(os.Stdout, child.Process.Pid)
			os.Exit(0)
		case "--pid1-proof-descendant":
			signal.Ignore(syscall.SIGTERM)
			wake := make(chan os.Signal, 1)
			signal.Notify(wake, syscall.SIGUSR1)
			if err := os.WriteFile(filepath.Join("/proof", os.Args[2]+".pid"), []byte(fmt.Sprint(os.Getpid())), 0600); err != nil {
				panic(err)
			}
			for range wake {
			}
		}
	}
	os.Exit(m.Run())
}

func pid1ProofNative(mode string) int {
	// Native runtimes can close inherited descriptors. The outer container fence
	// must contain them independently of whether this token survives exec.
	if err := unix.Close(3); err != nil {
		panic(err)
	}
	roles := []string{"group"}
	if mode != "native-exit" {
		roles = append(roles, "escaped")
	}
	for _, role := range roles {
		child := exec.Command(os.Args[0], "--pid1-proof-descendant", role)
		child.Stdout, child.Stderr = os.Stdout, os.Stderr
		child.SysProcAttr = &syscall.SysProcAttr{Setsid: role == "escaped"}
		if err := child.Start(); err != nil {
			panic(err)
		}
	}
	signals := make(chan os.Signal, 1)
	if mode == "escalate" {
		signal.Ignore(syscall.SIGTERM)
		signal.Notify(signals, syscall.SIGUSR1)
	} else {
		signal.Notify(signals, syscall.SIGTERM)
	}
	if err := os.WriteFile("/proof/native.pid", []byte(fmt.Sprint(os.Getpid())), 0600); err != nil {
		panic(err)
	}
	if mode == "native-exit" {
		var line string
		if _, err := fmt.Fscanln(os.Stdin, &line); err != nil || line != "exit" {
			return 124
		}
		return 23
	}
	<-signals
	return 0
}

func TestPID1ContainerContract(t *testing.T) {
	if os.Getpid() != 1 {
		t.Fatal("PID1 proof requires a private container PID namespace")
	}
	mode := os.Getenv("PID1_PROOF_MODE")
	if mode != "native-exit" && mode != "term" && mode != "escalate" && mode != "crash" {
		t.Fatalf("invalid PID1_PROOF_MODE %q", mode)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM)
	defer signal.Stop(signals)
	owner, err := os.OpenFile("/proof/owner", os.O_CREATE|os.O_RDONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer owner.Close()
	if err := unix.Flock(int(owner.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	var supervisor *Supervisor
	switch os.Getenv("PID1_PROOF_BACKEND") {
	case "pid1":
		supervisor, err = NewPID1Supervisor(owner)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := NewPID1Supervisor(nil); err == nil {
			t.Fatal("stateful PID1 backend accepted an absent owner descriptor")
		}
		mixed, err := NewSubprocessSupervisor(owner).NewProcess(ProcessConfig{Command: []string{os.Args[0], "--pid1-proof-fast"}})
		if err != nil {
			t.Fatal(err)
		}
		if err := mixed.Start(ctx); err == nil || !strings.Contains(err.Error(), "cannot be mixed") {
			t.Fatalf("mixed subprocess wait owner was not rejected: %v", err)
		}
		if mode == "native-exit" {
			t.Run("wait-ownership-and-orphan-reaping", func(t *testing.T) { provePID1Waits(t, ctx, supervisor) })
		}
	case "subprocess":
		supervisor = NewSubprocessSupervisor(owner)
	default:
		t.Fatal("PID1_PROOF_BACKEND must explicitly select pid1 or subprocess")
	}
	p, err := supervisor.NewProcess(ProcessConfig{
		Command: []string{os.Args[0], "--pid1-proof-native", mode},
		Env:     map[string]string{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Start(ctx); err != nil {
		t.Fatal(err)
	}
	if os.Getenv("PID1_PROOF_BACKEND") == "subprocess" {
		if _, err := NewPID1Supervisor(owner); err == nil || !strings.Contains(err.Error(), "subprocess supervisors own child waits") {
			t.Fatalf("PID1 reaper displaced an active subprocess wait owner: %v", err)
		}
	}
	readers := make(chan error, 2)
	for _, stream := range []io.Reader{p.Stdout(), p.Stderr()} {
		go func() { _, err := io.Copy(os.Stderr, stream); readers <- err }()
	}
	// The native's ready record is written only after it closed its owner FD.
	if err := waitPID1ProofFile(ctx, "/proof/native.pid"); err != nil {
		t.Fatal(err)
	}
	if err := waitPID1ProofFile(ctx, "/proof/group.pid"); err != nil {
		t.Fatal(err)
	}
	if mode != "native-exit" {
		if err := waitPID1ProofFile(ctx, "/proof/escaped.pid"); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile("/proof/ready", []byte("ready\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if mode == "native-exit" {
		if err := waitPID1ProofFile(ctx, "/proof/exit-native"); err != nil {
			t.Fatal(err)
		}
		if err := p.WriteLine(ctx, "exit"); err != nil {
			t.Fatal(err)
		}
	} else {
		select {
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		case <-signals:
		}
		if err := p.Stop(ctx, time.Second); err != nil {
			t.Fatal(err)
		}
	}
	code, err := p.Wait(ctx)
	if err != nil || mode == "native-exit" && code != 23 || mode == "term" && code != 0 || mode == "escalate" && code != 137 {
		t.Fatalf("native completion = (%d, %v)", code, err)
	}
	// The escaped descendant holds these pipes until container teardown; joining
	// its readers here would deadlock PID 1 instead of reaching that boundary.
	if mode == "native-exit" {
		for range 2 {
			select {
			case err := <-readers:
				if err != nil {
					t.Fatal(err)
				}
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
		}
	}
}

func provePID1Waits(t *testing.T, ctx context.Context, supervisor *Supervisor) {
	t.Helper()
	withoutStateOwner, err := NewPID1SupervisorWithoutStateOwner()
	if err != nil {
		t.Fatal(err)
	}
	failedStart, err := withoutStateOwner.NewProcess(ProcessConfig{
		Command: []string{"/run/agentplane-pid1-proof-absent"},
		Env:     map[string]string{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := failedStart.Start(ctx); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("state-less PID1 start did not report an exec failure: %v", err)
	}
	fast, err := withoutStateOwner.NewProcess(ProcessConfig{
		Command: []string{os.Args[0], "--pid1-proof-fast"},
		Env:     map[string]string{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := fast.Start(ctx); err != nil {
		t.Fatal(err)
	}
	if code, err := fast.Wait(ctx); code != 42 || err != nil {
		t.Fatalf("state-less PID1 result = (%d, %v)", code, err)
	}
	for _, stream := range []io.Reader{fast.Stdout(), fast.Stderr()} {
		if _, err := io.Copy(io.Discard, stream); err != nil {
			t.Fatal(err)
		}
	}

	// Fast exits race registration and SIGCHLD delivery. All native results must
	// retain their own status and caller-owned pipes through the central reaper.
	for range 32 {
		p, err := supervisor.NewProcess(ProcessConfig{Command: []string{os.Args[0], "--pid1-proof-fast"}})
		if err != nil {
			t.Fatal(err)
		}
		if err := p.Start(ctx); err != nil {
			t.Fatal(err)
		}
		if p.PID() <= 1 {
			t.Fatalf("fast native published an invalid PID: %d", p.PID())
		}
		code, err := p.Wait(ctx)
		if code != 42 || err != nil {
			t.Fatalf("fast native result = (%d, %v)", code, err)
		}
		out, outErr := io.ReadAll(p.Stdout())
		stderr, stderrErr := io.ReadAll(p.Stderr())
		if string(out) != "stdout" || string(stderr) != "stderr" || outErr != nil || stderrErr != nil {
			t.Fatalf("native streams = (%q, %q, %v, %v)", out, stderr, outErr, stderrErr)
		}
	}
	for range 8 {
		p, err := supervisor.NewProcess(ProcessConfig{Command: []string{os.Args[0], "--pid1-proof-orphan"}})
		if err != nil {
			t.Fatal(err)
		}
		if err := p.Start(ctx); err != nil {
			t.Fatal(err)
		}
		if code, err := p.Wait(ctx); code != 0 || err != nil {
			t.Fatalf("orphan parent result = (%d, %v)", code, err)
		}
		var orphan int
		if _, err := fmt.Fscan(p.Stdout(), &orphan); err != nil {
			t.Fatal(err)
		}
		ticker := time.NewTicker(10 * time.Millisecond)
		for !errors.Is(unix.Kill(orphan, 0), unix.ESRCH) {
			select {
			case <-ctx.Done():
				ticker.Stop()
				t.Fatal("adopted orphan was not reaped:", ctx.Err())
			case <-ticker.C:
			}
		}
		ticker.Stop()
	}
}

func waitPID1ProofFile(ctx context.Context, path string) error {
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		data, err := os.ReadFile(path)
		if err == nil && strings.TrimSpace(string(data)) != "" {
			return nil
		}
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
