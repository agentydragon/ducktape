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
	// An untrusted native can close the inherited token. The trusted supervisor
	// must retain its own descriptor independently of the native's behavior.
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
	signal.Notify(signals, syscall.SIGTERM)
	defer signal.Stop(signals)
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
	if mode != "native-exit" && mode != "term" && mode != "crash" {
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
	p, err := NewSubprocessSupervisor(owner).NewProcess(ProcessConfig{
		Command: []string{os.Args[0], "--pid1-proof-native", mode},
		Env:     map[string]string{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Start(ctx); err != nil {
		t.Fatal(err)
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
	if err != nil || mode == "native-exit" && code != 23 || mode == "term" && code != 0 {
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
