package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sessionclient "github.com/agentydragon/ducktape/agentplane/session_client"
	"github.com/google/uuid"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
)

const sessionsHelp = `Calls the SessionDispatcher gRPC API (AGENTPLANE_SESSIONS_URL, AGENTPLANE_SESSIONS_CREDENTIAL).
get and observe-input also read retained History (AGENTPLANE_HISTORY_URL, AGENTPLANE_HISTORY_CREDENTIAL).
read-turns reads retained History only.

This group opens and directs Sessions in an existing Sandbox and can observe an input's correlated
History result or snapshot. It does not provision an environment or read a live environment. The caller
must already be authorized by each backend it reads.`

type sessionCall func(context.Context, dispatcherpb.SessionDispatcherClient, []string) error

func (a *app) sessionsCommand() *cobra.Command {
	return group("sessions", "Open and direct Sessions in an existing Sandbox", sessionsHelp,
		a.openSessionCommand(), a.submitInputCommand(), a.getOpeningCommand(), a.getDispatchCommand(),
		a.cancelInputCommand(), a.interruptTurnCommand(), a.getSessionCommand(), a.readTurnsCommand(), a.observeInputCommand())
}

func (a *app) getSessionCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "get SESSION_ID",
		Short: "Read a Session opening and retained History summary",
		Long: `Reads SessionDispatcher.GetSessionOpening and HistoryService.GetSessionSummary independently.
The result keeps missing and unavailable sources distinct; it does not query a live environment.
Both backends authorize this caller independently.`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			if args[0] == "" {
				return usageFailure("SESSION_ID must not be empty")
			}
			sessionID, err := uuid.Parse(args[0])
			if err != nil {
				return usageFailure("SESSION_ID must be a UUID")
			}
			dispatcherConnection, err := a.connectGRPC("SESSIONS")
			if err != nil {
				return err
			}
			defer dispatcherConnection.Close()
			historyConnection, err := a.connectGRPC("HISTORY")
			if err != nil {
				return err
			}
			defer historyConnection.Close()
			ctx, cancel := a.deadline()
			defer cancel()
			result, err := sessionclient.GetSession(
				ctx,
				dispatcherpb.NewSessionDispatcherClient(dispatcherConnection),
				historypb.NewHistoryServiceClient(historyConnection),
				sessionID,
			)
			if err != nil {
				return grpcFailure(ctx, err)
			}
			body, err := json.Marshal(result)
			if err != nil {
				return invalidResponse("marshal Session snapshot: %v", err)
			}
			return a.showOne(body, sessionSnapshotLine)
		},
	}
}

func (a *app) readTurnsCommand() *cobra.Command {
	var count uint32 = 20
	var pageCursor string
	command := &cobra.Command{
		Use:   "read-turns SESSION_ID [--count N] [--page-cursor TOKEN]",
		Short: "Read a bounded page of retained Session turns",
		Long: `Reads retained History only, through a pinned cursor snapshot. Pages resume after complete turns;
an unfinished final turn is returned without a terminal status. Gaps and source errors remain explicit.
The page cursor is opaque, Session-scoped and carries no authority.`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			if args[0] == "" {
				return usageFailure("SESSION_ID must not be empty")
			}
			sessionID, err := uuid.Parse(args[0])
			if err != nil {
				return usageFailure("SESSION_ID must be a UUID")
			}
			if count < 1 || count > 100 {
				return usageFailure("--count must be between 1 and 100")
			}
			connection, err := a.connectGRPC("HISTORY")
			if err != nil {
				return err
			}
			defer connection.Close()
			ctx, cancel := a.deadline()
			defer cancel()
			result, err := sessionclient.ReadTurns(ctx, historypb.NewHistoryServiceClient(connection), sessionclient.ReadTurnsRequest{
				SessionID: sessionID, Count: count, PageCursor: pageCursor,
			})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			body, err := json.Marshal(result)
			if err != nil {
				return invalidResponse("marshal retained turns: %v", err)
			}
			return a.showOne(body, turnPageLine)
		},
	}
	command.Flags().Uint32Var(&count, "count", count, "maximum completed turns to return (1-100)")
	command.Flags().StringVar(&pageCursor, "page-cursor", "", "opaque cursor returned by the prior page for this Session")
	return command
}

func (a *app) observeInputCommand() *cobra.Command {
	var waitSeconds int
	var resumeToken string
	var commandID string
	command := &cobra.Command{
		Use:   "observe-input SESSION_ID --command-id ID [--wait-seconds N] [--resume-token TOKEN]",
		Short: "Read one input's Dispatcher receipt and correlated History result",
		Long: `Reads the caller-held input command from SessionDispatcher, then follows retained History from its
admission cursor. It does not submit or retry input. A returned not_observed result is not evidence that
the command failed or that a turn completed. History gaps and source errors are returned as structured
coverage facts. Both backends authorize this caller independently.`,
		Args: cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			if args[0] == "" || commandID == "" {
				return usageFailure("SESSION_ID and --command-id must be non-empty")
			}
			if waitSeconds < 0 || waitSeconds > 30 {
				return usageFailure("--wait-seconds must be between 0 and 30")
			}
			dispatcherConnection, err := a.connectGRPC("SESSIONS")
			if err != nil {
				return err
			}
			defer dispatcherConnection.Close()
			historyConnection, err := a.connectGRPC("HISTORY")
			if err != nil {
				return err
			}
			defer historyConnection.Close()
			ctx, cancel := a.deadline()
			defer cancel()
			result, err := sessionclient.ObserveInput(
				ctx,
				dispatcherpb.NewSessionDispatcherClient(dispatcherConnection),
				historypb.NewHistoryServiceClient(historyConnection),
				sessionclient.ObserveInputRequest{
					SessionID: args[0], CommandID: commandID, WaitSeconds: waitSeconds, ResumeToken: resumeToken,
				},
			)
			if err != nil {
				if deadlineElapsed(ctx) {
					body, marshalErr := json.Marshal(result)
					if marshalErr != nil {
						return invalidResponse("marshal input observation: %v", marshalErr)
					}
					if showErr := a.showOne(body, inputObservationLine); showErr != nil {
						return showErr
					}
					return exitWith(exitDeadline)
				}
				return grpcFailure(ctx, err)
			}
			body, err := json.Marshal(result)
			if err != nil {
				return invalidResponse("marshal input observation: %v", err)
			}
			if err := a.showOne(body, inputObservationLine); err != nil {
				return err
			}
			if deadlineElapsed(ctx) && result.Observation.Kind == sessionclient.ObservationNotObserved {
				return exitWith(exitDeadline)
			}
			return nil
		},
	}
	command.Flags().StringVar(&commandID, "command-id", "", "caller-held idempotency key used by submit-input")
	command.Flags().IntVar(&waitSeconds, "wait-seconds", 0, "maximum time to wait for a correlated result (0 reads once; max 30)")
	command.Flags().StringVar(&resumeToken, "resume-token", "", "resume token returned by a prior observation for this Session and command")
	_ = command.MarkFlagRequired("command-id")
	return command
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

func (a *app) readSessionSpec(source, additionalInstructions string) (*sessionpb.SessionSpec, error) {
	data, err := a.readSessionValue("--spec", source)
	if err != nil {
		return nil, err
	}
	spec := &sessionpb.SessionSpec{}
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

func inputObservationLine(raw json.RawMessage) (string, error) {
	var observation sessionclient.InputObservation
	if err := json.Unmarshal(raw, &observation); err != nil {
		return "", invalidResponse("%v", err)
	}
	line := fields(observation.SessionID, observation.CommandID, string(observation.Observation.Kind))
	if observation.Dispatch.Kind != "" {
		line += fields("dispatcher " + observation.Dispatch.Kind)
	}
	if len(observation.Dispatch.Value) > 0 {
		dispatch := &dispatcherpb.Dispatch{}
		if err := protojson.Unmarshal(observation.Dispatch.Value, dispatch); err != nil {
			return "", invalidResponse("dispatcher receipt: %v", err)
		}
		line += fields(enumSuffix(dispatch.GetState().String(), "DELIVERY_STATE_"), enumSuffix(dispatch.GetOperation().String(), "OPERATION_"))
	}
	if observation.Observation.TurnID != nil {
		line += fields("turn " + *observation.Observation.TurnID)
	}
	if observation.Observation.Turn != nil {
		turn := observation.Observation.Turn
		line += fields("turn " + turn.TurnID)
		line += fields("status " + turn.Status)
		if turn.FinalAssistantMessage != nil {
			line += fields("message", strconv.Quote(firstLine(*turn.FinalAssistantMessage)))
		}
	}
	if observation.Observation.Reason != nil {
		line += fields("reason", strconv.Quote(*observation.Observation.Reason))
	}
	if observation.Observation.ThroughCursor != "" {
		line += fields("history through " + observation.Observation.ThroughCursor)
	}
	if len(observation.Observation.Gaps) > 0 {
		line += fmt.Sprintf("  history gaps %d", len(observation.Observation.Gaps))
	}
	if observation.Dispatch.Error != nil {
		line += fields("dispatcher error", observation.Dispatch.Error.Code)
	}
	if observation.Observation.Error != nil {
		line += fields("history error", observation.Observation.Error.Code)
	}
	return line, nil
}

func sessionSnapshotLine(raw json.RawMessage) (string, error) {
	snapshot, err := decode[sessionclient.SessionSnapshot](raw)
	if err != nil {
		return "", err
	}
	line := fields(snapshot.SessionID.String(), "dispatcher "+string(snapshot.Opening.Kind), "history "+string(snapshot.History.Kind))
	if snapshot.Opening.Error != nil {
		line += fields("dispatcher error " + snapshot.Opening.Error.Code)
	}
	if snapshot.History.Error != nil {
		line += fields("history error " + snapshot.History.Error.Code)
	}
	if len(snapshot.History.Value) > 0 {
		summary := &historypb.SessionSummary{}
		if err := protojson.Unmarshal(snapshot.History.Value, summary); err != nil {
			return "", invalidResponse("History summary: %v", err)
		}
		line += fields("history cursor "+strconv.FormatUint(summary.GetCommittedCursor(), 10), enumSuffix(summary.GetFeedStatus().String(), "FEED_STATUS_"))
	}
	return line, nil
}

func turnPageLine(raw json.RawMessage) (string, error) {
	page, err := decode[sessionclient.TurnPage](raw)
	if err != nil {
		return "", err
	}
	line := fields(page.SessionID.String(), "history through "+page.ThroughCursor, fmt.Sprintf("%d turns", len(page.Turns)))
	if page.NextCursor != "" {
		line += fields("more turns available")
	}
	if len(page.Gaps) > 0 {
		line += fields(fmt.Sprintf("history gaps %d", len(page.Gaps)))
	}
	if page.Error != nil {
		line += fields("history error " + page.Error.Code)
	}
	for _, turn := range page.Turns {
		statusText := "status not observed"
		if turn.Status != nil {
			statusText = string(*turn.Status)
		}
		line += "\n" + fields(string(turn.TurnID), statusText, fmt.Sprintf("%d inputs", len(turn.Input)), fmt.Sprintf("%d tool calls", len(turn.ToolCalls)))
		if turn.FinalAssistantMessage != nil {
			line += fields("message", strconv.Quote(firstLine(*turn.FinalAssistantMessage)))
		}
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
