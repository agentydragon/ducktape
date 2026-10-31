package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
)

const sessionsHelp = `Calls the SessionDispatcher gRPC API (AGENTPLANE_SESSIONS_URL, AGENTPLANE_SESSIONS_CREDENTIAL).

This group opens and directs Sessions in an existing Sandbox. It does not provision an environment or
read the combined History snapshot. The Dispatcher caller must already be authorized by the backend.`

type sessionCall func(context.Context, dispatcherpb.SessionDispatcherClient, []string) error

func (a *app) sessionsCommand() *cobra.Command {
	return group("sessions", "Open and direct Sessions in an existing Sandbox", sessionsHelp,
		a.openSessionCommand(), a.submitInputCommand(), a.getOpeningCommand(), a.getDispatchCommand(),
		a.cancelInputCommand(), a.interruptTurnCommand())
}

func (a *app) sessionCommand(fn sessionCall) func(*cobra.Command, []string) error {
	return func(_ *cobra.Command, args []string) error {
		connection, err := a.connectGRPC("SESSIONS")
		if err != nil {
			return err
		}
		defer connection.Close()
		ctx, cancel := a.deadline()
		defer cancel()
		return fn(ctx, dispatcherpb.NewSessionDispatcherClient(connection), args)
	}
}

func (a *app) openSessionCommand() *cobra.Command {
	var sandbox, sandboxUID, specSource, additionalInstructions, setupScript string
	var setupScriptSet bool
	command := &cobra.Command{
		Use:   "open SESSION_ID --sandbox NAME --sandbox-uid UID --spec JSON|@FILE|- [--setup-script TEXT|@FILE|-] [--additional-instructions TEXT]",
		Short: "Open a Session in an existing Sandbox",
		Long: `Calls SessionDispatcher.OpenSession. SESSION_ID is supplied by the caller and is the idempotency key.
--spec is a SessionSpec in ProtoJSON. --additional-instructions appends standing context to any
instructions in the spec, separated by a blank line. Opening a Session does not create a model turn:
the caller must send it separately with submit-input. A setup script may execute during opening, but
is not model input. After an unknown outcome, use get-opening with the same SESSION_ID. The command
prints the Dispatcher opening record; it does not wait for History.`,
		Args: cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" {
				return usageFailure("SESSION_ID must not be empty")
			}
			if sandbox == "" || sandboxUID == "" || specSource == "" {
				return usageFailure("--sandbox, --sandbox-uid and --spec are required")
			}
			if specSource == "-" && setupScript == "-" {
				return usageFailure("only one of --spec and --setup-script may read stdin")
			}
			spec, err := a.readSessionSpec(specSource, additionalInstructions)
			if err != nil {
				return err
			}
			request := &dispatcherpb.OpenSessionRequest{
				SessionId: args[0], Sandbox: sandbox, SandboxUid: sandboxUID, Spec: spec,
			}
			if setupScriptSet {
				contents, err := a.readSessionValue("--setup-script", setupScript)
				if err != nil {
					return err
				}
				request.SetupScript = &contents
			}
			response, err := client.OpenSession(ctx, request)
			if err != nil {
				return grpcFailure(ctx, err)
			}
			if response.GetOpening() == nil {
				return invalidResponse("OpenSession returned no opening")
			}
			body, err := protoJSON(response.GetOpening())
			if err != nil {
				return err
			}
			return a.showOne(body, sessionOpeningLine)
		}),
	}
	command.PreRun = func(cmd *cobra.Command, _ []string) {
		setupScriptSet = cmd.Flags().Changed("setup-script")
	}
	command.Flags().StringVar(&sandbox, "sandbox", "", "name of the existing Sandbox")
	command.Flags().StringVar(&sandboxUID, "sandbox-uid", "", "expected Kubernetes Sandbox UID")
	command.Flags().StringVar(&specSource, "spec", "", "SessionSpec ProtoJSON, @FILE or - for stdin")
	command.Flags().StringVar(&setupScript, "setup-script", "", "setup script text, @FILE or - for stdin; may execute during opening")
	command.Flags().StringVar(&additionalInstructions, "additional-instructions", "", "append standing context to spec.instructions")
	for _, flag := range []string{"sandbox", "sandbox-uid", "spec"} {
		_ = command.MarkFlagRequired(flag)
	}
	return command
}

func (a *app) submitInputCommand() *cobra.Command {
	var sandbox, sandboxUID, commandID, text string
	command := &cobra.Command{
		Use:   "submit-input SESSION_ID --sandbox NAME --sandbox-uid UID --command-id ID --text TEXT",
		Short: "Queue input for a Session",
		Long: `Calls SessionDispatcher.SubmitInput. The caller supplies COMMAND_ID; it is an idempotency key and is
never generated or replaced by this CLI. The response is the Dispatcher record, not proof that the
runner received the input. After an unknown outcome, use get-dispatch with the same command ID.`,
		Args: cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" || sandbox == "" || sandboxUID == "" || commandID == "" {
				return usageFailure("SESSION_ID, --sandbox, --sandbox-uid and --command-id must be non-empty")
			}
			response, err := client.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
				SessionId: args[0], Sandbox: sandbox, SandboxUid: sandboxUID, CommandId: commandID, Text: text,
			})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			if response.GetDispatch() == nil {
				return invalidResponse("SubmitInput returned no dispatch")
			}
			body, err := protoJSON(response.GetDispatch())
			if err != nil {
				return err
			}
			return a.showOne(body, dispatchLine)
		}),
	}
	command.Flags().StringVar(&sandbox, "sandbox", "", "name of the existing Sandbox")
	command.Flags().StringVar(&sandboxUID, "sandbox-uid", "", "expected Kubernetes Sandbox UID")
	command.Flags().StringVar(&commandID, "command-id", "", "caller-held idempotency key for this input")
	command.Flags().StringVar(&text, "text", "", "text to submit to the Session")
	for _, flag := range []string{"sandbox", "sandbox-uid", "command-id", "text"} {
		_ = command.MarkFlagRequired(flag)
	}
	return command
}

func (a *app) getOpeningCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "get-opening SESSION_ID",
		Short: "Read the Dispatcher opening record",
		Long:  "Calls SessionDispatcher.GetSessionOpening. This is only the Dispatcher record; it is not a combined History snapshot.",
		Args:  cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" {
				return usageFailure("SESSION_ID must not be empty")
			}
			response, err := client.GetSessionOpening(ctx, &dispatcherpb.GetSessionOpeningRequest{SessionId: args[0]})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			if response.GetOpening() == nil {
				return invalidResponse("GetSessionOpening returned no opening")
			}
			body, err := protoJSON(response.GetOpening())
			if err != nil {
				return err
			}
			return a.showOne(body, sessionOpeningLine)
		}),
	}
}

func (a *app) getDispatchCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "get-dispatch COMMAND_ID",
		Short: "Read a Dispatcher command record",
		Long:  "Calls SessionDispatcher.GetDispatch. Use the caller-held COMMAND_ID to inspect an input's authoritative Dispatcher receipt after an unknown outcome.",
		Args:  cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" {
				return usageFailure("COMMAND_ID must not be empty")
			}
			response, err := client.GetDispatch(ctx, &dispatcherpb.GetDispatchRequest{CommandId: args[0]})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			if response.GetDispatch() == nil {
				return invalidResponse("GetDispatch returned no dispatch")
			}
			body, err := protoJSON(response.GetDispatch())
			if err != nil {
				return err
			}
			return a.showOne(body, dispatchLine)
		}),
	}
}

func (a *app) cancelInputCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "cancel-input COMMAND_ID",
		Short: "Cancel a queued input",
		Long: `Calls SessionDispatcher.CancelDispatch for the caller-held input COMMAND_ID. It only cancels while the
Dispatcher still has the input queued; once delivery starts, the runner owns the outcome. After an
unknown outcome, use get-dispatch with the same COMMAND_ID.`,
		Args: cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" {
				return usageFailure("COMMAND_ID must not be empty")
			}
			response, err := client.CancelDispatch(ctx, &dispatcherpb.CancelDispatchRequest{CommandId: args[0]})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			if response.GetDispatch() == nil {
				return invalidResponse("CancelDispatch returned no dispatch")
			}
			body, err := protoJSON(response.GetDispatch())
			if err != nil {
				return err
			}
			return a.showOne(body, dispatchLine)
		}),
	}
}

func (a *app) interruptTurnCommand() *cobra.Command {
	var sandbox, sandboxUID, commandID, turnID string
	command := &cobra.Command{
		Use:   "interrupt-turn SESSION_ID --sandbox NAME --sandbox-uid UID --command-id ID --turn-id ID",
		Short: "Interrupt the Session's current turn",
		Long: `Calls SessionDispatcher.InterruptTurn directly on a Ready Sandbox. COMMAND_ID is caller-held and is
required for correlation; it is not generated or replaced by this CLI. This call is not queued.`,
		Args: cobra.ExactArgs(1),
		RunE: a.sessionCommand(func(ctx context.Context, client dispatcherpb.SessionDispatcherClient, args []string) error {
			if args[0] == "" || sandbox == "" || sandboxUID == "" || commandID == "" || turnID == "" {
				return usageFailure("SESSION_ID, --sandbox, --sandbox-uid, --command-id and --turn-id must be non-empty")
			}
			response, err := client.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
				SessionId: args[0], Sandbox: sandbox, SandboxUid: sandboxUID, CommandId: commandID, TurnId: turnID,
			})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			if response == nil {
				return invalidResponse("InterruptTurn returned no response")
			}
			body, err := protoJSON(response)
			if err != nil {
				return err
			}
			return a.showOne(body, interruptLine)
		}),
	}
	command.Flags().StringVar(&sandbox, "sandbox", "", "name of the existing Sandbox")
	command.Flags().StringVar(&sandboxUID, "sandbox-uid", "", "expected Kubernetes Sandbox UID")
	command.Flags().StringVar(&commandID, "command-id", "", "caller-held correlation ID")
	command.Flags().StringVar(&turnID, "turn-id", "", "ID of the turn to interrupt")
	for _, flag := range []string{"sandbox", "sandbox-uid", "command-id", "turn-id"} {
		_ = command.MarkFlagRequired(flag)
	}
	return command
}

func (a *app) readSessionSpec(source, additionalInstructions string) (*runnerpb.SessionSpec, error) {
	data, err := a.readSessionValue("--spec", source)
	if err != nil {
		return nil, err
	}
	spec := &runnerpb.SessionSpec{}
	if err := protojson.Unmarshal([]byte(data), spec); err != nil {
		return nil, usageFailure("--spec: %v", err)
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

func (a *app) readSessionValue(flag, source string) (string, error) {
	var data []byte
	var err error
	switch {
	case source == "-":
		data, err = io.ReadAll(a.stdin)
	case strings.HasPrefix(source, "@"):
		data, err = os.ReadFile(source[1:])
	default:
		data = []byte(source)
	}
	if err != nil {
		return "", usageFailure("%s: %v", flag, err)
	}
	return string(data), nil
}

func sessionOpeningLine(raw json.RawMessage) (string, error) {
	opening := &dispatcherpb.SessionOpening{}
	if err := protojson.Unmarshal(raw, opening); err != nil {
		return "", invalidResponse("%v", err)
	}
	if opening.GetSessionId() == "" {
		return "", invalidResponse("no session ID in the opening")
	}
	line := fields(
		opening.GetSessionId(),
		enumSuffix(opening.GetState().String(), "DELIVERY_STATE_"),
		opening.GetSandbox(),
		"Kubernetes Sandbox UID "+opening.GetSandboxUid(),
	)
	if opening.Reason != nil {
		line += fields("reason", fmt.Sprintf("%q", opening.GetReason()))
	}
	return line, nil
}

func dispatchLine(raw json.RawMessage) (string, error) {
	dispatch := &dispatcherpb.Dispatch{}
	if err := protojson.Unmarshal(raw, dispatch); err != nil {
		return "", invalidResponse("%v", err)
	}
	if dispatch.GetCommandId() == "" {
		return "", invalidResponse("no command ID in the dispatch")
	}
	line := fields(
		dispatch.GetCommandId(),
		enumSuffix(dispatch.GetState().String(), "DELIVERY_STATE_"),
		enumSuffix(dispatch.GetOperation().String(), "OPERATION_"),
		dispatch.GetSessionId(),
		dispatch.GetSandbox(),
		"Kubernetes Sandbox UID "+dispatch.GetSandboxUid(),
	)
	if dispatch.Reason != nil {
		line += fields("reason", fmt.Sprintf("%q", dispatch.GetReason()))
	}
	return line, nil
}

func interruptLine(raw json.RawMessage) (string, error) {
	response := &dispatcherpb.InterruptTurnResponse{}
	if err := protojson.Unmarshal(raw, response); err != nil {
		return "", invalidResponse("%v", err)
	}
	line := enumSuffix(response.GetState().String(), "DELIVERY_STATE_")
	if response.Reason != nil {
		line += fields("reason", fmt.Sprintf("%q", response.GetReason()))
	}
	return line, nil
}
