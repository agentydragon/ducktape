package cli

import (
	"io"

	"github.com/agentydragon/ducktape/agentplane/cli/internal/actions"
	"github.com/agentydragon/ducktape/agentplane/cli/internal/environments"
	"github.com/agentydragon/ducktape/agentplane/cli/internal/history"
	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	"github.com/agentydragon/ducktape/agentplane/cli/internal/notifications"
	sessiondispatcher "github.com/agentydragon/ducktape/agentplane/cli/internal/session_dispatcher"
	"github.com/spf13/cobra"
)

type app struct{ *invocation.Invocation }

func newApp(getenv func(string) string, stdin io.Reader, stdout, stderr io.Writer) *app {
	return &app{Invocation: invocation.New(getenv, stdin, stdout, stderr)}
}

// Execute runs one command line with the supplied environment and streams.
func Execute(args []string, getenv func(string) string, stdin io.Reader, stdout, stderr io.Writer) int {
	return newApp(getenv, stdin, stdout, stderr).execute(args)
}
func (a *app) execute(args []string) int { return a.Execute(a.root(), args) }

const rootHelp = `agentplane calls the Action Service, Session Dispatcher and History Service APIs. Notifications and egress rules use gRPC through local sidecar routes.
The environments group reads the EnvironmentService discovery API. It adds no authority and keeps
no state. The 'agentplane actions guide' command prints Action workflow guides bundled in this
image without a service call. Each live group reads its AGENTPLANE_*_URL; when a
AGENTPLANE_*_CREDENTIAL is configured, the CLI sends it as bearer metadata. A group fails alone with
exit 2 when its URL is missing.

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
		RunE:          invocation.RequireSubcommand,
	}
	root.SetOut(a.Stdout())
	root.SetErr(a.Stderr())
	root.CompletionOptions.DisableDefaultCmd = true
	a.BindFlags(root)
	root.AddCommand(actions.Command(a.Invocation), sessiondispatcher.Command(a.Invocation), history.Command(a.Invocation), notifications.Command(a.Invocation), a.egressCommand(), environments.Command(a.Invocation))
	return root
}
