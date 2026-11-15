package invocation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strconv"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/spf13/cobra"
)

const defaultTimeout = 30 * time.Second

// Invocation is one call: its environment, streams and the flags every command shares.
type Invocation struct {
	getenv         func(string) string
	stdin          io.Reader
	stdout, stderr io.Writer

	asJSON  bool
	timeout seconds
	// ctx ends when SIGINT or SIGTERM arrives. Commands run under a deadline derived from it.
	ctx context.Context
}

func New(getenv func(string) string, stdin io.Reader, stdout, stderr io.Writer) *Invocation {
	return &Invocation{getenv: getenv, stdin: stdin, stdout: stdout, stderr: stderr, timeout: seconds{defaultTimeout}}
}

// Execute runs one command line and returns the process exit status.
func (a *Invocation) Execute(root *cobra.Command, args []string) int {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)
	var received atomic.Int32
	go func() {
		select {
		case caught := <-signals:
			received.Store(int32(caught.(syscall.Signal)))
			cancel()
		case <-ctx.Done():
		}
	}()
	a.ctx = ctx

	root.SetArgs(append([]string{}, args...)) // cobra reads os.Args when given nil
	err := root.ExecuteContext(ctx)

	var done Outcome
	var failed *Failure
	switch {
	case err == nil:
		return 0
	case errors.Is(err, ErrInterrupted):
		fmt.Fprintln(a.stderr, "interrupted; request outcome may be unknown")
		return 128 + int(received.Load())
	case errors.As(err, &done):
		return int(done)
	case errors.As(err, &failed):
	default:
		// Cobra's own: an unknown flag or command, or the wrong number of arguments.
		failed = UsageFailure("%v", err)
	}
	a.report(failed)
	return failed.Exit
}

// report writes the error envelope on stderr: JSON under --json, one line otherwise.
func (a *Invocation) report(f *Failure) {
	if a.asJSON {
		encoder := json.NewEncoder(a.stderr)
		encoder.SetEscapeHTML(false)
		_ = encoder.Encode(Envelope{Error: f.APIError})
		return
	}
	line := fmt.Sprintf("error: %s (%s", f.Code, f.Source)
	if f.HTTPStatus != nil {
		line += fmt.Sprintf(", http %d", *f.HTTPStatus)
	}
	if f.Reason != nil {
		line += ", reason " + *f.Reason
	}
	line += "): " + f.Message
	if f.RetryAfterSeconds != nil {
		line += fmt.Sprintf(" (retry after %ds)", *f.RetryAfterSeconds)
	}
	fmt.Fprintln(a.stderr, line)
}

// Deadline bounds one command by --timeout; 0 sets no deadline.
func (a *Invocation) Deadline() (context.Context, context.CancelFunc) {
	if a.timeout.d == 0 {
		return context.WithCancel(a.ctx)
	}
	return context.WithTimeout(a.ctx, a.timeout.d)
}

// seconds is a duration flag that reads a bare number as seconds ("30", "2.5") or a Go duration ("30s").
type seconds struct{ d time.Duration }

func (s *seconds) Set(value string) error {
	if number, err := strconv.ParseFloat(value, 64); err == nil {
		s.d = time.Duration(number * float64(time.Second))
	} else if s.d, err = time.ParseDuration(value); err != nil {
		return errors.New("want a number of seconds or a duration such as 30s")
	}
	if s.d < 0 {
		return errors.New("must not be negative")
	}
	return nil
}

func (s *seconds) String() string { return s.d.String() }

func (s *seconds) Type() string { return "seconds" }

// BindFlags attaches the invocation-owned global flags before parsing.
func (a *Invocation) BindFlags(root *cobra.Command) {
	root.PersistentFlags().BoolVar(&a.asJSON, "json", false, "print the service's JSON unmodified; lists as JSON Lines")
	root.PersistentFlags().Var(&a.timeout, "timeout", "deadline of the whole call in seconds; 0 sets none and waiting commands read once")
}

func (a *Invocation) Getenv(name string) string { return a.getenv(name) }
func (a *Invocation) Stdin() io.Reader          { return a.stdin }
func (a *Invocation) Stdout() io.Writer         { return a.stdout }
func (a *Invocation) Stderr() io.Writer         { return a.stderr }
func (a *Invocation) JSON() bool                { return a.asJSON }

// Context is canceled by a signal, independently of the command deadline, so
// Actions can reconcile an uncertain submission with its existing recovery budget.
func (a *Invocation) Context() context.Context { return a.ctx }
