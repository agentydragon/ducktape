package sessiondispatcher

import (
	"context"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/proto/sessionplacement"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/dispatchstate"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
)

const dispatcherHelp = `Calls the SessionDispatcher gRPC API (AGENTPLANE_SESSIONS_URL; optional AGENTPLANE_SESSIONS_CREDENTIAL bearer).
These commands operate on existing Environments and use the Dispatcher's caller authorization.
For combined Dispatcher and History reads, use agentplane actions guide session_delegation.`

func (a *commands) dispatcherCommand() *cobra.Command {
	return invocation.Group("session-dispatcher", "Call the Session Dispatcher", dispatcherHelp,
		a.createSessionCommand(), a.listSessionsCommand(), a.submitInputCommand(), a.getDispatchCommand(),
		a.cancelInputCommand(), a.interruptTurnCommand())
}

func (a *commands) sessionCommand(run func(context.Context, dispatcherpb.SessionDispatcherClient, []string) error) func(*cobra.Command, []string) error {
	return invocation.GRPCCommand(a.Invocation, "SESSIONS", dispatcherpb.NewSessionDispatcherClient, run)
}

func (a *commands) createSessionCommand() *cobra.Command {
	var sandbox, sandboxUID, specSource, additionalInstructions, setupScript string
	var setupScriptSet bool
	command := &cobra.Command{
		Use:   "create SESSION_ID --sandbox NAME --sandbox-uid UID --spec JSON|@FILE|- [--setup-script TEXT|@FILE|-] [--additional-instructions TEXT]",
		Short: "Create a Session in an existing Sandbox",
		Long: `Calls SessionDispatcher.CreateSession. SESSION_ID is a caller-supplied canonical UUID and is the idempotency key.
--spec is a SessionSpec in ProtoJSON. --additional-instructions appends standing context to any
instructions in the spec, separated by a blank line. Creating a Session does not create a model turn:
the caller must send it separately with submit-input. A setup script may execute during materialization, but
is not model input. After an unknown outcome, use the sessions/get Action with the same SESSION_ID. The command
prints the Dispatcher Session resource; it does not wait for History.`,
		Args: cobra.ExactArgs(1),
		PreRunE: func(cmd *cobra.Command, args []string) error {
			setupScriptSet = cmd.Flags().Changed("setup-script")
			if id, err := uuid.Parse(args[0]); err != nil || id.String() != args[0] {
				return invocation.UsageFailure("SESSION_ID must be a canonical UUID")
			}
			return nil
		},
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if sandbox == "" || sandboxUID == "" || specSource == "" {
				return invocation.UsageFailure("--sandbox, --sandbox-uid and --spec are required")
			}
			if specSource == "-" && setupScript == "-" {
				return invocation.UsageFailure("only one of --spec and --setup-script may read stdin")
			}
			spec, err := a.readSessionSpec(specSource, additionalInstructions)
			if err != nil {
				return err
			}
			request := &dispatcherpb.CreateSessionRequest{
				SessionId: args[0], Spec: spec,
			}
			target := sessionplacement.Target(spec.GetPlacement())
			if target.GetName() != sandbox || target.GetUid() != sandboxUID {
				return invocation.UsageFailure("--spec placement must match --sandbox and --sandbox-uid")
			}
			if setupScriptSet {
				contents, err := a.readSessionValue("--setup-script", setupScript)
				if err != nil {
					return err
				}
				request.SetupScript = &contents
			}
			response, err := client.CreateSession(ctx, request)
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response.GetSession() == nil {
				return invocation.InvalidResponse("CreateSession returned no Session")
			}
			return invocation.ShowProto(a.Invocation, []*dispatcherpb.Session{response.GetSession()}, sessionLine, protojson.MarshalOptions{})
		}),
	}
	command.Flags().StringVar(&sandbox, "sandbox", "", "name of the existing Sandbox")
	command.Flags().StringVar(&sandboxUID, "sandbox-uid", "", "expected Kubernetes Sandbox UID")
	command.Flags().StringVar(&specSource, "spec", "", "SessionSpec ProtoJSON, @FILE or - for stdin")
	command.Flags().StringVar(&setupScript, "setup-script", "", "setup script text, @FILE or - for stdin; may execute during materialization")
	command.Flags().StringVar(&additionalInstructions, "additional-instructions", "", "append standing context to spec.instructions")
	for _, flag := range []string{"sandbox", "sandbox-uid", "spec"} {
		_ = command.MarkFlagRequired(flag)
	}
	return command
}

func (a *commands) submitInputCommand() *cobra.Command {
	var commandID, text string
	command := &cobra.Command{
		Use:   "submit-input SESSION_ID --command-id ID --text TEXT",
		Short: "Queue input for a Session",
		Long: `Calls SessionDispatcher.SubmitInput. The caller supplies COMMAND_ID; it is an idempotency key and is
never generated or replaced by this CLI. The response is the Dispatcher record, not proof that the
runner received the input. After an unknown outcome, use get-dispatch with the same command ID.`,
		Args: cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" || commandID == "" {
				return invocation.UsageFailure("SESSION_ID and --command-id must be non-empty")
			}
			response, err := client.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
				SessionId: args[0], CommandId: commandID, Text: text,
			})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response.GetDispatch() == nil {
				return invocation.InvalidResponse("SubmitInput returned no dispatch")
			}
			return invocation.ShowProto(a.Invocation, []*dispatcherpb.Dispatch{response.GetDispatch()}, dispatchLine, protojson.MarshalOptions{})
		}),
	}
	command.Flags().StringVar(&commandID, "command-id", "", "caller-held idempotency key for this input")
	command.Flags().StringVar(&text, "text", "", "text to submit to the Session")
	for _, flag := range []string{"command-id", "text"} {
		_ = command.MarkFlagRequired(flag)
	}
	return command
}

func (a *commands) listSessionsCommand() *cobra.Command {
	var limit uint32
	var pageToken string
	command := &cobra.Command{
		Use:   "list [--limit N] [--after TOKEN]",
		Short: "Discover current Dispatcher Sessions",
		Long: `Lists one bounded page in Session ID order, including waiting and failed Sessions.
This reads the Dispatcher only: the environment and History may be unavailable or have no record yet.
The caller sees only records authorized by its retained owner identity or configured backend authority.
This is not retained History or environment-free central Session discovery.`,
		Args: cobra.NoArgs,
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, _ []string) error {
			if limit < 1 || limit > 100 {
				return invocation.UsageFailure("--limit must be between 1 and 100")
			}
			response, err := client.ListSessions(ctx, &dispatcherpb.ListSessionsRequest{Limit: limit, PageToken: pageToken})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if a.JSON() {
				return invocation.ShowProto(a.Invocation, []*dispatcherpb.ListSessionsResponse{response}, nil, protojson.MarshalOptions{})
			}
			if err := invocation.ShowProto(a.Invocation, response.GetSessions(), sessionLine, protojson.MarshalOptions{}); err != nil {
				return err
			}
			if response.GetNextPageToken() != "" {
				fmt.Fprintf(a.Stderr(), "more may follow: --after %s\n", response.GetNextPageToken())
			}
			return nil
		}),
	}
	command.Flags().Uint32Var(&limit, "limit", 50, "maximum Sessions returned (1-100)")
	command.Flags().StringVar(&pageToken, "after", "", "continue with the previous page's nextPageToken")
	return command
}

func (a *commands) getDispatchCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "get-dispatch COMMAND_ID",
		Short: "Read a Dispatcher command record",
		Long:  "Calls SessionDispatcher.GetDispatch. Use the caller-held COMMAND_ID to inspect an input's authoritative Dispatcher receipt after an unknown outcome.",
		Args:  cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" {
				return invocation.UsageFailure("COMMAND_ID must not be empty")
			}
			response, err := client.GetDispatch(ctx, &dispatcherpb.GetDispatchRequest{CommandId: args[0]})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response.GetDispatch() == nil {
				return invocation.InvalidResponse("GetDispatch returned no dispatch")
			}
			return invocation.ShowProto(a.Invocation, []*dispatcherpb.Dispatch{response.GetDispatch()}, dispatchLine, protojson.MarshalOptions{})
		}),
	}
}

func (a *commands) cancelInputCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "cancel-input COMMAND_ID",
		Short: "Cancel an input before delivery begins",
		Long: `Calls SessionDispatcher.CancelDispatch for the caller-held input COMMAND_ID. It only cancels while the
Dispatcher has not begun forwarding the input. QUEUED alone does not prove cancellability after a
lost reply. After an unknown outcome, use get-dispatch with the same COMMAND_ID.`,
		Args: cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" {
				return invocation.UsageFailure("COMMAND_ID must not be empty")
			}
			response, err := client.CancelDispatch(ctx, &dispatcherpb.CancelDispatchRequest{CommandId: args[0]})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response.GetDispatch() == nil {
				return invocation.InvalidResponse("CancelDispatch returned no dispatch")
			}
			return invocation.ShowProto(a.Invocation, []*dispatcherpb.Dispatch{response.GetDispatch()}, dispatchLine, protojson.MarshalOptions{})
		}),
	}
}

func (a *commands) interruptTurnCommand() *cobra.Command {
	var commandID, turnID string
	command := &cobra.Command{
		Use:   "interrupt-turn SESSION_ID --command-id ID --turn-id ID",
		Short: "Interrupt the Session's current turn",
		Long: `Queues an interrupt for the exact turn. COMMAND_ID is caller-held and is
the idempotency key; it is not generated or replaced by this CLI.`,
		Args: cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" || commandID == "" || turnID == "" {
				return invocation.UsageFailure("SESSION_ID, --command-id and --turn-id must be non-empty")
			}
			response, err := client.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
				SessionId: args[0], CommandId: commandID, TurnId: turnID,
			})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response.GetDispatch() == nil {
				return invocation.InvalidResponse("InterruptTurn returned no dispatch")
			}
			return invocation.ShowProto(a.Invocation, []*dispatcherpb.Dispatch{response.GetDispatch()}, dispatchLine, protojson.MarshalOptions{})
		}),
	}
	command.Flags().StringVar(&commandID, "command-id", "", "caller-held idempotency key")
	command.Flags().StringVar(&turnID, "turn-id", "", "ID of the turn to interrupt")
	for _, flag := range []string{"command-id", "turn-id"} {
		_ = command.MarkFlagRequired(flag)
	}
	return command
}

func (a *commands) readSessionSpec(source, additionalInstructions string) (*sessionpb.SessionSpec, error) {
	data, err := a.readSessionValue("--spec", source)
	if err != nil {
		return nil, err
	}
	spec := &sessionpb.SessionSpec{}
	if err := protojson.Unmarshal([]byte(data), spec); err != nil {
		return nil, invocation.UsageFailure("--spec: %v", err)
	}
	if additionalInstructions != "" {
		instructions := strings.TrimRight(spec.Instructions, "\r\n")
		if instructions != "" {
			instructions += "\n\n"
		}
		spec.Instructions = instructions + additionalInstructions
	}
	return spec, nil
}

func (a *commands) readSessionValue(flag, source string) (string, error) {
	var data []byte
	var err error
	switch {
	case source == "-":
		data, err = io.ReadAll(a.Stdin())
	case strings.HasPrefix(source, "@"):
		data, err = os.ReadFile(source[1:])
	default:
		data = []byte(source)
	}
	if err != nil {
		return "", invocation.UsageFailure("%s: %v", flag, err)
	}
	return string(data), nil
}

func sessionLine(session *dispatcherpb.Session) (string, error) {
	if session.GetSessionId() == "" {
		return "", invocation.InvalidResponse("no session ID in the Session")
	}
	if session.GetStatus() == nil {
		return "", invocation.InvalidResponse("no materialization status in the Session")
	}
	target := sessionplacement.Target(session.GetPlacement())
	if target.GetName() == "" || target.GetUid() == "" {
		return "", invocation.InvalidResponse("no exact environment target in the Session")
	}
	line := invocation.Fields(
		session.GetSessionId(),
		invocation.EnumSuffix(session.GetStatus().GetMaterialization().String(), "SESSION_MATERIALIZATION_"),
		target.GetName(),
		"Kubernetes Sandbox UID "+target.GetUid(),
	)
	if session.GetStatus().Reason != nil {
		line += invocation.Fields("reason", fmt.Sprintf("%q", session.GetStatus().GetReason()))
	}
	return line, nil
}

func dispatchLine(dispatch *dispatcherpb.Dispatch) (string, error) {
	if dispatch.GetCommandId() == "" {
		return "", invocation.InvalidResponse("no command ID in the dispatch")
	}
	line := invocation.Fields(
		dispatch.GetCommandId(),
		invocation.EnumSuffix(dispatchstate.NormalizeDeliveryState(dispatch.GetState()).String(), "DELIVERY_STATE_"),
		invocation.EnumSuffix(dispatch.GetOperation().String(), "OPERATION_"),
		dispatch.GetSessionId(),
		dispatch.GetSandbox(),
		"Kubernetes Sandbox UID "+dispatch.GetSandboxUid(),
	)
	if dispatch.Reason != nil {
		line += invocation.Fields("reason", fmt.Sprintf("%q", dispatch.GetReason()))
	}
	return line, nil
}

type commands struct{ *invocation.Invocation }

func Command(in *invocation.Invocation) *cobra.Command {
	return (&commands{Invocation: in}).dispatcherCommand()
}
