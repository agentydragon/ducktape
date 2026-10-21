package sandbox

import (
	"context"
	"errors"
	"io"
	"os"
	osexec "os/exec"
	"path/filepath"
	"reflect"
	"syscall"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/types"
	utilexec "k8s.io/client-go/util/exec"
)

// localExec runs the command on this host where the kubelet would run it in a container, and ends as an exec stream
// does: with ctx's error once ctx ends, and otherwise with the command's exit code, which for a process a signal
// ended is 128 plus the signal, as the container runtime reports it.
func localExec(ctx context.Context, _ types.NamespacedName, _ string, command []string, stdout, stderr io.Writer) error {
	run := osexec.CommandContext(ctx, command[0], command[1:]...)
	run.Stdout, run.Stderr = stdout, stderr
	err := run.Run()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	var exited *osexec.ExitError
	if !errors.As(err, &exited) {
		return err
	}
	status := exited.Sys().(syscall.WaitStatus)
	if status.Signaled() {
		return utilexec.CodeExitError{Err: err, Code: 128 + int(status.Signal())}
	}
	return utilexec.CodeExitError{Err: err, Code: status.ExitStatus()}
}

var testPod = types.NamespacedName{Namespace: "test-namespace", Name: "test-pod"}

func TestAScriptEndsAsItsCommandDid(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "work dir")
	if err := os.Mkdir(directory, 0o755); err != nil {
		t.Fatal(err)
	}
	const minute, kept = 60, 1000
	for _, test := range []struct {
		name   string
		script Script
		exit   Exit
		stdout Output
		stderr Output
	}{
		{
			name:   "exits zero",
			script: Script{Text: "echo out; echo err >&2", TimeoutSeconds: minute, MaxOutputBytes: kept},
			exit:   Exited{}, stdout: Text("out\n"), stderr: Text("err\n"),
		},
		{
			name:   "exits nonzero",
			script: Script{Text: "exit 3", TimeoutSeconds: minute, MaxOutputBytes: kept},
			exit:   Exited{ExitCode: 3}, stdout: Text(""), stderr: Text(""),
		},
		{
			name:   "outlives its timeout",
			script: Script{Text: "sleep 60", TimeoutSeconds: 1, MaxOutputBytes: kept},
			exit:   TimedOut{}, stdout: Text(""), stderr: Text(""),
		},
		{
			name:   "is killed",
			script: Script{Text: "kill -KILL $$", TimeoutSeconds: minute, MaxOutputBytes: kept},
			exit:   Killed{Signal: 9}, stdout: Text(""), stderr: Text(""),
		},
		{
			name:   "writes more than is kept",
			script: Script{Text: "printf 0123456789", TimeoutSeconds: minute, MaxOutputBytes: 4},
			exit:   Exited{}, stdout: Truncated{TruncatedText: "0123", TotalBytes: 10}, stderr: Text(""),
		},
		{
			name:   "writes bytes that are not UTF-8",
			script: Script{Text: `printf 'a\377b'`, TimeoutSeconds: minute, MaxOutputBytes: kept},
			exit:   Exited{}, stdout: Text("a\uFFFDb"), stderr: Text(""),
		},
		{
			// The directory reaches the shell as an argument, so it needs no quoting and the script sees none.
			name:   "starts from a directory",
			script: Script{Text: `pwd; echo "$#"`, Cwd: &directory, TimeoutSeconds: minute, MaxOutputBytes: kept},
			exit:   Exited{}, stdout: Text(directory + "\n0\n"), stderr: Text(""),
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()
			// A grace past every timeout here, so each ends as the command did.
			result, err := test.script.in(t.Context(), localExec, testPod, "workspace", time.Minute)
			if err != nil {
				t.Fatal(err)
			}
			if result.Exit != test.exit || !reflect.DeepEqual([]Output{result.Stdout, result.Stderr}, []Output{test.stdout, test.stderr}) {
				t.Errorf("ran to %#v with stdout %#v and stderr %#v; want %#v, %#v and %#v",
					result.Exit, result.Stdout, result.Stderr, test.exit, test.stdout, test.stderr)
			}
		})
	}
}

func TestAStreamThatOutlivesTheTimeoutAndGraceIsATimedOutScript(t *testing.T) {
	stuck := func(ctx context.Context, _ types.NamespacedName, _ string, _ []string, _, _ io.Writer) error {
		<-ctx.Done()
		return ctx.Err()
	}
	result, err := Script{Text: "true", TimeoutSeconds: 1}.in(t.Context(), stuck, testPod, "workspace", 0)
	if err != nil {
		t.Fatal(err)
	}
	if result.Exit != (TimedOut{}) {
		t.Errorf("exit = %#v, want TimedOut", result.Exit)
	}
}

// An exec whose caller stopped waiting has no outcome of its own to report: the script may still be running.
func TestAnExecItsCallerStoppedWaitingForIsNotAResult(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	_, err := Script{Text: "true", TimeoutSeconds: 60}.in(ctx, localExec, testPod, "workspace", 0)
	if !errors.Is(err, context.Canceled) {
		t.Errorf("err = %v, want context.Canceled", err)
	}
}

func TestAnExecKubernetesCouldNotRunIsAnExecError(t *testing.T) {
	refused := errors.New("test exec refused")
	unrun := func(context.Context, types.NamespacedName, string, []string, io.Writer, io.Writer) error {
		return refused
	}
	_, err := Script{Text: "true", TimeoutSeconds: 60}.in(t.Context(), unrun, testPod, "workspace", 0)
	var execError *ExecError
	if !errors.As(err, &execError) || !errors.Is(err, refused) {
		t.Errorf("err = %v, want an ExecError wrapping the exec's", err)
	}
}
