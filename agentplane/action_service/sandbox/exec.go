package sandbox

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/remotecommand"
	utilexec "k8s.io/client-go/util/exec"
)

// Exec runs command in a container of a Pod and streams its output to stdout and stderr. It returns nil when the
// command exits zero, an error that is a k8s.io/client-go/util/exec.ExitError when it exits otherwise, and any other
// error when Kubernetes could not run it. It returns once ctx ends, when the stream can still be writing.
type Exec func(ctx context.Context, pod types.NamespacedName, container string, command []string, stdout, stderr io.Writer) error

// KubernetesExec is Exec over pods/exec, through client-go's WebSocket executor. The API server authorizes its GET
// upgrade as both `get` and `create` on pods/exec.
func KubernetesExec(config *rest.Config, client kubernetes.Interface) Exec {
	return func(ctx context.Context, pod types.NamespacedName, container string, command []string, stdout, stderr io.Writer) error {
		request := client.CoreV1().RESTClient().Post().Resource("pods").Namespace(pod.Namespace).Name(pod.Name).
			SubResource("exec").VersionedParams(&corev1.PodExecOptions{
			Container: container, Command: command, Stdout: true, Stderr: true,
		}, scheme.ParameterCodec)
		executor, err := remotecommand.NewWebSocketExecutor(config, "GET", request.URL().String())
		if err != nil {
			return err
		}
		return executor.StreamWithContext(ctx, remotecommand.StreamOptions{Stdout: stdout, Stderr: stderr})
	}
}

// ExecError is an exec Kubernetes could not carry out. A command that ran and exited nonzero is not one: that is an
// ordinary ExecResult.
type ExecError struct {
	Err error
}

func (e *ExecError) Error() string { return "Kubernetes could not run the command: " + e.Err.Error() }

func (e *ExecError) Unwrap() error { return e.Err }

// Script is one bounded Bash script to run in a box.
type Script struct {
	Text string
	// Cwd is where it starts; nil for the container's own working directory.
	Cwd            *string
	TimeoutSeconds int
	// MaxOutputBytes is how much of each of stdout and stderr the result keeps.
	MaxOutputBytes int
}

// ExecResult is how one script run ended, and what it wrote.
type ExecResult struct {
	Exit            Exit    `json:"exit"`
	Stdout          Output  `json:"stdout"`
	Stderr          Output  `json:"stderr"`
	DurationSeconds float64 `json:"duration_seconds"`
}

// Exit is how a script ended: Exited, TimedOut or Killed.
type Exit interface{ isExit() }

type Exited struct {
	ExitCode int
}

// TimedOut is a script stopped at its timeout.
type TimedOut struct{}

// Killed is a script a signal ended.
type Killed struct {
	Signal int
}

func (Exited) isExit()   {}
func (TimedOut) isExit() {}
func (Killed) isExit()   {}

func (e Exited) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Kind     string `json:"kind"`
		ExitCode int    `json:"exit_code"`
	}{"exited", e.ExitCode})
}

func (TimedOut) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Kind string `json:"kind"`
	}{"timed_out"})
}

func (k Killed) MarshalJSON() ([]byte, error) {
	return json.Marshal(struct {
		Kind   string `json:"kind"`
		Signal int    `json:"signal"`
	}{"killed", k.Signal})
}

// exitStatus reads the exit code of `timeout`, which ends a script it stopped with 124, and a shell's convention for
// a process a signal ended.
func exitStatus(code int) Exit {
	switch {
	case code == 124:
		return TimedOut{}
	case code >= 128:
		return Killed{Signal: code - 128}
	default:
		return Exited{ExitCode: code}
	}
}

// Output is what a stream wrote: Text, or Truncated when it wrote more than the result keeps.
type Output interface{ isOutput() }

type Text string

// Truncated is the first bytes of a stream that wrote more than the result keeps, and how many it wrote.
type Truncated struct {
	TruncatedText string `json:"truncated_text"`
	TotalBytes    int    `json:"total_bytes"`
}

func (Text) isOutput()      {}
func (Truncated) isOutput() {}

// capture keeps the first limit bytes written to it and counts them all. An exec whose context ended can still be
// writing to it.
type capture struct {
	limit int

	mu    sync.Mutex
	kept  []byte
	total int
}

func (c *capture) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.total += len(p)
	c.kept = append(c.kept, p[:min(len(p), max(c.limit-len(c.kept), 0))]...)
	return len(p), nil
}

// output is what the stream wrote, its bytes read as UTF-8 with each invalid sequence replaced.
func (c *capture) output() Output {
	c.mu.Lock()
	defer c.mu.Unlock()
	text := strings.ToValidUTF8(string(c.kept), "�")
	if c.total > len(c.kept) {
		return Truncated{TruncatedText: text, TotalBytes: c.total}
	}
	return Text(text)
}

// command is the script under a remote timeout, from its Cwd when it names one. `timeout` and `bash` resolve on the
// container's PATH: the Nix-built images keep coreutils in /bin and have no /usr/bin/timeout.
func (s Script) command() []string {
	command := []string{"timeout", "--signal=TERM", "--kill-after=5s", fmt.Sprintf("%ds", s.TimeoutSeconds), "bash", "-lc"}
	if s.Cwd == nil {
		return append(command, s.Text)
	}
	// The directory reaches the shell as its first argument, which needs no quoting, and is shifted off before the
	// script runs.
	return append(command, "cd -- \"$1\"; shift\n"+s.Text, "bash", *s.Cwd)
}

// in runs the script in a container of pod, and stops waiting for it grace after its timeout.
func (s Script) in(ctx context.Context, exec Exec, pod types.NamespacedName, container string, grace time.Duration) (ExecResult, error) {
	stdout, stderr := &capture{limit: s.MaxOutputBytes}, &capture{limit: s.MaxOutputBytes}
	started := time.Now()
	running, stop := context.WithTimeout(ctx, time.Duration(s.TimeoutSeconds)*time.Second+grace)
	defer stop()
	err := exec(running, pod, container, s.command(), stdout, stderr)
	var exit Exit
	var exited utilexec.ExitError
	switch {
	case err == nil:
		exit = Exited{}
	case errors.As(err, &exited):
		exit = exitStatus(exited.ExitStatus())
	case ctx.Err() != nil:
		return ExecResult{}, err
	case running.Err() != nil:
		exit = TimedOut{}
	default:
		return ExecResult{}, &ExecError{err}
	}
	return ExecResult{
		Exit: exit, Stdout: stdout.output(), Stderr: stderr.output(), DurationSeconds: time.Since(started).Seconds(),
	}, nil
}
