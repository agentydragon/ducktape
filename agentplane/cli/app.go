package main

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

// app is one invocation: its environment, streams and the flags every command shares.
type app struct {
	getenv         func(string) string
	stdin          io.Reader
	stdout, stderr io.Writer

	asJSON  bool
	timeout seconds
	// ctx ends when SIGINT or SIGTERM arrives. Commands run under a deadline derived from it.
	ctx context.Context
}

func newApp(getenv func(string) string, stdin io.Reader, stdout, stderr io.Writer) *app {
	return &app{getenv: getenv, stdin: stdin, stdout: stdout, stderr: stderr, timeout: seconds{defaultTimeout}}
}

// execute runs one command line and returns the process exit status.
func (a *app) execute(args []string) int {
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

	root := a.root()
	root.SetArgs(append([]string{}, args...)) // cobra reads os.Args when given nil
	err := root.ExecuteContext(ctx)

	var done outcome
	var failed *failure
	switch {
	case err == nil:
		return 0
	case errors.Is(err, errInterrupted):
		fmt.Fprintln(a.stderr, "interrupted; request outcome may be unknown")
		return 128 + int(received.Load())
	case errors.As(err, &done):
		return int(done)
	case errors.As(err, &failed):
	default:
		// Cobra's own: an unknown flag or command, or the wrong number of arguments.
		failed = usageFailure("%v", err)
	}
	a.report(failed)
	return failed.exit
}

// report writes the error envelope on stderr: JSON under --json, one line otherwise.
func (a *app) report(f *failure) {
	if a.asJSON {
		encoder := json.NewEncoder(a.stderr)
		encoder.SetEscapeHTML(false)
		_ = encoder.Encode(envelope{Error: f.apiError})
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

// deadline bounds one command by --timeout; 0 sets no deadline.
func (a *app) deadline() (context.Context, context.CancelFunc) {
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

const rootHelp = `agentplane calls the Action Service and Session Dispatcher APIs, reads History for sessions get, read-turns,
and observe-input, and calls the HTTP APIs a Sandbox already reaches through the egress proxy: the Notification Service
(notifications) and the Egress rules API (egress). The environments group reads the co-hosted Runner Gateway discovery
API. It adds no authority and keeps no state. Each group reads its AGENTPLANE_*_URL and AGENTPLANE_*_CREDENTIAL pair
(the placeholder bearer) and fails alone with exit 2 when they are missing; environments uses AGENTPLANE_SESSIONS_URL
and AGENTPLANE_SESSIONS_CREDENTIAL.

With --json stdout is the service's own JSON, unmodified: one value, or one per line for a list. Text
is for reading, not parsing. stdout is the answer and stderr the diagnostics; after exit 1-5 or 15
stdout is empty. Nothing prompts or depends on a terminal.

Exit status is the platform's answer, never a payload's: an Action that succeeded exits 0 whatever
its result says (a script's exit code, an MCP isError).

  0       success
  1       refused or invalid, or an unclassified error: report, do not retry unchanged
  2       usage or configuration error, found before any request
  3       transient (unreachable, deadline, rate limit, draining): retry a read; reconcile a write by its ID
  4       conflict: repeated idempotency key, stale subscription version, cancelled key
  5       not found, or not owned by this account
  10      --timeout elapsed before the condition held; the current receipt is on stdout
  11      Action denied          12  failed without a result    13  cancelled
  14      execution_unknown: check the effects, never replay
  15      submit lost its response and a lookup by its key found nothing: run
          "actions get --key KEY" later, never resubmit
  16      cancel: too_late, dispatch was already claimed
  17      cancel: already_finished
  128+N   ended by signal N; request outcome may be unknown`

func (a *app) root() *cobra.Command {
	root := &cobra.Command{
		Use:           "agentplane",
		Short:         "Call Agentplane APIs and discover existing environments from a Sandbox",
		Long:          rootHelp,
		SilenceUsage:  true,
		SilenceErrors: true,
		Args:          cobra.NoArgs,
		RunE:          requireSubcommand,
	}
	root.SetOut(a.stdout)
	root.SetErr(a.stderr)
	root.CompletionOptions.DisableDefaultCmd = true
	root.PersistentFlags().BoolVar(&a.asJSON, "json", false, "print the service's JSON unmodified; lists as JSON Lines")
	root.PersistentFlags().Var(&a.timeout, "timeout", "deadline of the whole call in seconds; 0 sets none and waiting commands read once")
	root.AddCommand(a.actionsCommand(), a.sessionsCommand(), a.notificationsCommand(), a.egressCommand(), a.environmentsCommand())
	return root
}

func requireSubcommand(cmd *cobra.Command, _ []string) error {
	fmt.Fprint(cmd.ErrOrStderr(), cmd.UsageString())
	return usageFailure("%s needs a command", cmd.CommandPath())
}

// group is a command that only holds subcommands.
func group(use, short, long string, children ...*cobra.Command) *cobra.Command {
	command := &cobra.Command{Use: use, Short: short, Long: long, Args: cobra.NoArgs, RunE: requireSubcommand}
	command.AddCommand(children...)
	return command
}

// call is the body of a command: the connected service and the positional arguments, under the
// call's deadline.
type call func(ctx context.Context, svc *service, args []string) error

// command connects the group, applies --timeout and runs fn.
func (a *app) command(groupName string, fn call) func(*cobra.Command, []string) error {
	return func(_ *cobra.Command, args []string) error {
		svc, err := a.connect(groupName)
		if err != nil {
			return err
		}
		ctx, cancel := a.deadline()
		defer cancel()
		return fn(ctx, svc, args)
	}
}
