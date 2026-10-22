package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
)

// recoveryTimeout bounds the one lookup that follows a submit whose response was lost. It is its
// own budget because the deadline may be what lost the response.
const recoveryTimeout = 10 * time.Second

const actionsHelp = `Calls the generated ActionService client (AGENTPLANE_ACTIONS_URL, AGENTPLANE_ACTIONS_CREDENTIAL).

Discover with groups, list, show and policy; submit an Action and follow it with submit --wait, wait,
result, get and events; withdraw it with cancel. Approval is not success: wait and result exit 0 only
for a succeeded Action, 11 denied, 12 failed, 13 cancelled, 14 execution_unknown, 10 when --timeout
ran out first. Whatever a succeeded Action's result says, the exit status is 0.`

func (a *app) actionsCommand() *cobra.Command {
	return group("actions", "Discover, submit, follow and cancel Actions", actionsHelp,
		a.groupsCommand(), a.listCommand(), a.showCommand(), a.policyCommand(), a.submitCommand(),
		a.getCommand(), a.resultCommand(), a.waitCommand(), a.requestsCommand(), a.eventsCommand(), a.cancelCommand())
}

func protoJSON(message proto.Message) ([]byte, error) {
	encoded, err := protojson.Marshal(message)
	if err != nil {
		return nil, invalidResponse("%v", err)
	}
	return encoded, nil
}

func protoJSONList[T proto.Message](messages []T) ([]json.RawMessage, error) {
	items := make([]json.RawMessage, 0, len(messages))
	for _, message := range messages {
		encoded, err := protoJSON(message)
		if err != nil {
			return nil, err
		}
		items = append(items, encoded)
	}
	return items, nil
}

func catalog(ctx context.Context, client action_servicepb.ActionServiceClient) ([]json.RawMessage, error) {
	response, err := client.ListActionGroups(ctx, &action_servicepb.ListActionGroupsRequest{})
	if err != nil {
		return nil, grpcFailure(ctx, err)
	}
	return protoJSONList(response.GetActionGroups())
}

func (a *app) groupsCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "groups",
		Short: "List ActionGroups",
		Long:  "Calls ActionService.ListActionGroups. --json prints one ActionGroup per line, each with every Action's description and input schema.",
		Args:  cobra.NoArgs,
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, _ []string) error {
			groups, err := catalog(ctx, client)
			if err != nil {
				return err
			}
			return a.show(groups, groupLine)
		}),
	}
}

func (a *app) listCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "list [GROUP]",
		Short: "List Actions, one per line",
		Long:  "Calls ActionService.ListActionGroups, flattened to one Action per line, of GROUP if given. An unknown GROUP exits 5.",
		Args:  cobra.MaximumNArgs(1),
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, args []string) error {
			groups, err := catalog(ctx, client)
			if err != nil {
				return err
			}
			var actions []json.RawMessage
			found := len(args) == 0
			for _, raw := range groups {
				group, err := decode[struct {
					Key     string            `json:"key"`
					Actions []json.RawMessage `json:"actions"`
				}](raw)
				if err != nil {
					return err
				}
				if len(args) == 0 || group.Key == args[0] {
					actions = append(actions, group.Actions...)
					found = true
				}
			}
			if !found {
				return clientFailure(exitNotFound, "NOT_FOUND", "no ActionGroup %q", args[0])
			}
			return a.show(actions, actionLine)
		}),
	}
}

// splitAction parses GROUP/ACTION.
func splitAction(spec string) (group, name string, err error) {
	group, name, found := strings.Cut(spec, "/")
	if !found || group == "" || name == "" || strings.Contains(name, "/") {
		return "", "", usageFailure("want GROUP/ACTION, got %q", spec)
	}
	return group, name, nil
}

func (a *app) showCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "show GROUP/ACTION",
		Short: "Show one Action's argument schema",
		Long:  "Calls ActionService.GetAction: the Action's description and input schema.",
		Args:  cobra.ExactArgs(1),
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, args []string) error {
			group, name, err := splitAction(args[0])
			if err != nil {
				return err
			}
			response, err := client.GetAction(ctx, &action_servicepb.GetActionRequest{GroupKey: group, ActionKey: name})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			body, err := protoJSON(response.GetAction())
			if err != nil {
				return err
			}
			return a.showOne(body, actionDetail)
		}),
	}
}

func (a *app) policyCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "policy",
		Short: "Show this account's Action auto-approval policy",
		Long:  "Calls ActionService.GetActionPolicy for the ServiceAccount the credential proves.",
		Args:  cobra.NoArgs,
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, _ []string) error {
			response, err := client.GetActionPolicy(ctx, &action_servicepb.GetActionPolicyRequest{})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			body, err := protoJSON(response.GetActionPolicy())
			if err != nil {
				return err
			}
			return a.showOne(body, policyText)
		}),
	}
}

// target is the request a command is about: by ID, or by the idempotency key it was submitted under.
type target struct{ id, key string }

func newTarget(args []string, key string) (target, error) {
	if (len(args) == 1) == (key != "") {
		return target{}, usageFailure("give exactly one of ID or --key KEY")
	}
	if len(args) == 1 {
		return target{id: args[0]}, nil
	}
	return target{key: key}, nil
}

func (t target) read(ctx context.Context, client action_servicepb.ActionServiceClient) (*receipt, error) {
	if t.id != "" {
		return readReceipt(ctx, client, t.id)
	}
	return lookupByKey(ctx, client, t.key)
}

func (a *app) printReceipt(r *receipt) error {
	raw, err := protoJSON(r.request)
	if err != nil {
		return err
	}
	return a.showOne(raw, receiptLine)
}

func (a *app) getCommand() *cobra.Command {
	var key string
	command := &cobra.Command{
		Use:   "get {ID | --key KEY}",
		Short: "Read an Action request",
		Long: `Calls ActionService.GetActionRequest, or ListActionRequests by idempotency key. Prints the receipt and
exits 0 whatever its state; use wait or result to follow it. Exit 5 if there is no such request.`,
		Args: cobra.MaximumNArgs(1),
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, args []string) error {
			t, err := newTarget(args, key)
			if err != nil {
				return err
			}
			r, err := t.read(ctx, client)
			if err != nil {
				return err
			}
			return a.printReceipt(r)
		}),
	}
	command.Flags().StringVar(&key, "key", "", "the idempotency key the request was submitted under")
	return command
}

func (a *app) waitCommand() *cobra.Command {
	var key, until string
	command := &cobra.Command{
		Use:   "wait {ID | --key KEY} [--until decision|terminal]",
		Short: "Wait for an Action request to be decided or to finish",
		Long: `Follows ActionService.WatchActionRequest within --timeout (default 30s; 0 reads once). Prints the receipt.
--until terminal (the default) waits for succeeded, failed, denied, cancelled or execution_unknown; decision
waits for the state to leave decision_pending.

Exit 0 only if the condition holds and the Action did not fail: 11 denied, 12 failed, 13 cancelled,
14 execution_unknown, 10 if --timeout ran out first. If the service is unreachable when it does, exit 3
with the last receipt read on stdout. Ending the watch never cancels the Action.`,
		Args: cobra.MaximumNArgs(1),
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, args []string) error {
			t, err := newTarget(args, key)
			if err != nil {
				return err
			}
			condition, err := parseUntil(until)
			if err != nil {
				return err
			}
			last, err := await(ctx, client, condition, nil, func(ctx context.Context) (*receipt, error) { return t.read(ctx, client) })
			if err != nil {
				return a.afterWait(last, err)
			}
			return a.finish(last, condition)
		}),
	}
	command.Flags().StringVar(&key, "key", "", "the idempotency key the request was submitted under")
	command.Flags().StringVar(&until, "until", "terminal", "decision or terminal")
	return command
}

func (a *app) resultCommand() *cobra.Command {
	var key string
	command := &cobra.Command{
		Use:   "result {ID | --key KEY}",
		Short: "Wait for an Action to finish and print its result",
		Long: `Follows ActionService.WatchActionRequest within --timeout, then, for a succeeded Action, prints its
execution.result: the MCP tool's CallToolResult, or the sandbox executor's JSON. Text mode prints the
result's text blocks, or indented JSON when there are none. For any other state it prints the receipt,
so the reason (a Decision note, execution.error) is visible.

Exit codes are wait's. A succeeded Action exits 0 whatever its result says: read the result to
learn a script's own exit code or an MCP isError.`,
		Args: cobra.MaximumNArgs(1),
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, args []string) error {
			t, err := newTarget(args, key)
			if err != nil {
				return err
			}
			last, err := await(ctx, client, untilTerminal, nil, func(ctx context.Context) (*receipt, error) { return t.read(ctx, client) })
			if err != nil {
				return a.afterWait(last, err)
			}
			if last.request.GetState() == action_servicepb.ActionState_ACTION_STATE_SUCCEEDED {
				return a.printResult(last)
			}
			return a.finish(last, untilTerminal)
		}),
	}
	command.Flags().StringVar(&key, "key", "", "the idempotency key the request was submitted under")
	return command
}

func (a *app) printResult(r *receipt) error {
	result := []byte("null")
	if execution := r.request.GetExecution(); execution != nil && execution.GetResult() != nil {
		var err error
		result, err = protoJSON(execution.GetResult())
		if err != nil {
			return err
		}
	}
	return a.showOne(result, resultText)
}

func (a *app) submitCommand() *cobra.Command {
	var key, title, description, arguments, until string
	var wait bool
	command := &cobra.Command{
		Use:   "submit GROUP/ACTION --title TEXT [--arguments JSON|@FILE|-] [--wait [--until decision|terminal]]",
		Short: "Submit an Action request",
		Long: `Calls ActionService.SubmitActionRequest. --arguments is sent as an object and validated by the service against
the Action's current schema; read it first with show. --title is one line for the operator (60 characters);
--description adds detail.

Without --key a random idempotency key is generated and written to stderr before submission. A repeated
key is refused (exit 4), so the key is the only handle on a request whose response is lost: retry a
failed submit with --key KEY, never without it, and recover with "get --key KEY". The CLI sends one
submission per key. If the response is lost it looks the key up once: found, it carries on as if the response
had arrived; not found, it exits 15 (never resubmit; look again later).

Without --wait it prints the receipt and exits 0 once the request is recorded, whatever its state.
--wait then follows WatchActionRequest, with its options and exit codes.`,
		Args: cobra.ExactArgs(1),
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, args []string) error {
			group, name, err := splitAction(args[0])
			if err != nil {
				return err
			}
			condition, err := parseUntil(until)
			if err != nil {
				return err
			}
			if !wait && until != "terminal" {
				return usageFailure("--until needs --wait")
			}
			parsedArguments, err := a.readArguments(arguments)
			if err != nil {
				return err
			}
			if key == "" {
				key = uuid.NewString()
				a.announceKey(key)
			}
			request := &action_servicepb.SubmitActionRequestRequest{
				IdempotencyKey: key, Action: &action_servicepb.ActionIdentity{Group: group, Name: name},
				Arguments: parsedArguments, Title: title,
			}
			if description != "" {
				request.Description = &description
			}
			created, err := a.post(ctx, client, request)
			if err != nil {
				return err
			}
			if !wait {
				return a.printReceipt(created)
			}
			last, err := await(ctx, client, condition, created, nil)
			if err != nil {
				return a.afterWait(last, err)
			}
			return a.finish(last, condition)
		}),
	}
	command.Flags().StringVar(&key, "key", "", "idempotency key; generated and printed on stderr if absent")
	command.Flags().StringVar(&title, "title", "", "one line saying what the request does, for the operator (required)")
	command.Flags().StringVar(&description, "description", "", "optional detail the title leaves out")
	command.Flags().StringVar(&arguments, "arguments", "", "the Action's arguments: JSON object, @FILE or - for stdin")
	command.Flags().BoolVar(&wait, "wait", false, "then wait for the outcome, as the wait command")
	command.Flags().StringVar(&until, "until", "terminal", "with --wait: decision or terminal")
	_ = command.MarkFlagRequired("title")
	return command
}

func (a *app) readArguments(spec string) (*structpb.Struct, error) {
	var data []byte
	var err error
	switch {
	case spec == "":
		return structpb.NewStruct(map[string]any{})
	case spec == "-":
		data, err = io.ReadAll(a.stdin)
	case strings.HasPrefix(spec, "@"):
		data, err = os.ReadFile(spec[1:])
	default:
		data = []byte(spec)
	}
	if err != nil {
		return nil, usageFailure("--arguments: %v", err)
	}
	var object map[string]any
	if err := json.Unmarshal(data, &object); err != nil || object == nil {
		return nil, usageFailure("--arguments must be a JSON object")
	}
	return structpb.NewStruct(object)
}

func (a *app) announceKey(key string) {
	if a.asJSON {
		fmt.Fprintf(a.stderr, "{\"idempotency_key\": %q}\n", key)
	} else {
		fmt.Fprintf(a.stderr, "idempotency key: %s\n", key)
	}
}

// post makes the one submission. A failure that may have committed it is followed by one lookup under the same key.
func (a *app) post(ctx context.Context, client action_servicepb.ActionServiceClient, request *action_servicepb.SubmitActionRequestRequest) (*receipt, error) {
	response, err := client.SubmitActionRequest(ctx, request)
	if err == nil {
		created, parseErr := receiptFromProto(response.GetActionRequest())
		if parseErr == nil {
			return created, nil
		}
		unreadable := clientFailure(exitRefused, "INTERNAL", "%v", parseErr)
		unreadable.sent = true
		err = unreadable
	} else {
		err = grpcFailure(ctx, err)
	}
	var failed *failure
	if !errors.As(err, &failed) {
		return nil, err
	}
	switch {
	case failed.exit == exitConflict:
		failed.Message = fmt.Sprintf("%s; idempotency key %s is in use: read that request with `agentplane actions get --key %s`",
			failed.Message, request.GetIdempotencyKey(), request.GetIdempotencyKey())
	case failed.ambiguous():
		lookup, cancel := context.WithTimeout(a.ctx, recoveryTimeout)
		defer cancel()
		found, lookupErr := lookupByKey(lookup, client, request.GetIdempotencyKey())
		if errors.Is(lookupErr, errInterrupted) {
			return nil, lookupErr
		}
		if lookupErr == nil {
			return found, nil
		}
		var lookupFailure *failure
		if errors.As(lookupErr, &lookupFailure) && lookupFailure.Code == "NOT_FOUND" {
			failed.exit = exitSubmitLost
			failed.Message = fmt.Sprintf("%s; a lookup by the key found no request: look again later with `agentplane actions get --key %s`, and do not resubmit",
				failed.Message, request.GetIdempotencyKey())
			return nil, failed
		}
		return nil, clientFailure(exitTransient, failed.Code, "%s; submission outcome is unknown because lookup by the same key failed (%v): check `agentplane actions get --key %s` before retrying",
			failed.Message, lookupErr, request.GetIdempotencyKey())
	}
	return nil, failed
}

func actionState(value string) (action_servicepb.ActionState, error) {
	name := "ACTION_STATE_" + strings.ToUpper(value)
	state := action_servicepb.ActionState(action_servicepb.ActionState_value[name])
	if state == action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED {
		return 0, usageFailure("unknown Action state %q", value)
	}
	return state, nil
}

func (a *app) requestsCommand() *cobra.Command {
	var states []string
	var limit int
	var after string
	command := &cobra.Command{
		Use:   "requests [--state STATE]...",
		Short: "List this account's Action requests, newest first",
		Long: `Calls ActionService.ListActionRequests, following pages (128 at most) until --limit items (default 50; 0 is
unlimited). To continue, pass the last item's id as --after.`,
		Args: cobra.NoArgs,
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, _ []string) error {
			parsedStates := make([]action_servicepb.ActionState, 0, len(states))
			for _, text := range states {
				state, err := actionState(text)
				if err != nil {
					return err
				}
				parsedStates = append(parsedStates, state)
			}
			items, err := collect(limit, after, func(after string, size int) ([]json.RawMessage, error) {
				request := &action_servicepb.ListActionRequestsRequest{State: parsedStates}
				limit := int32(size)
				request.Limit = &limit
				if after != "" {
					request.AfterId = &after
				}
				response, err := client.ListActionRequests(ctx, request)
				if err != nil {
					return nil, grpcFailure(ctx, err)
				}
				return protoJSONList(response.GetActionRequests())
			}, idOf)
			if err != nil {
				return err
			}
			return showPage(a, items, limit, idOf, receiptLine)
		}),
	}
	command.Flags().StringArrayVar(&states, "state", nil, "only requests in this state; repeatable")
	command.Flags().IntVar(&limit, "limit", defaultLimit, "stop after this many items; 0 is unlimited")
	command.Flags().StringVar(&after, "after", "", "continue after this request id")
	return command
}

func (a *app) eventsCommand() *cobra.Command {
	var limit, after int
	command := &cobra.Command{
		Use:   "events ID [--after SEQUENCE]",
		Short: "List the state events of an Action request",
		Long: `Calls ActionService.ListActionRequestEvents, following pages (128 at most) until --limit items (default
50; 0 is unlimited). To continue, pass the last item's sequence as --after.`,
		Args: cobra.ExactArgs(1),
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, args []string) error {
			if after < 0 || after > int(1<<31-1) {
				return usageFailure("--after must be between 0 and 2147483647")
			}
			items, err := collect(limit, after, func(after, size int) ([]json.RawMessage, error) {
				request := &action_servicepb.ListActionRequestEventsRequest{RequestId: args[0], AfterSequence: int32(after)}
				limit := int32(size)
				request.Limit = &limit
				response, err := client.ListActionRequestEvents(ctx, request)
				if err != nil {
					return nil, grpcFailure(ctx, err)
				}
				return protoJSONList(response.GetEvents())
			}, sequenceOf)
			if err != nil {
				return err
			}
			return showPage(a, items, limit, sequenceOf, eventLine)
		}),
	}
	command.Flags().IntVar(&limit, "limit", defaultLimit, "stop after this many items; 0 is unlimited")
	command.Flags().IntVar(&after, "after", 0, "continue after this event sequence")
	return command
}

func (a *app) cancelCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "cancel ID",
		Short: "Cancel an Action request",
		Long: `Calls ActionService.CancelActionRequest. Prints the outcome and receipt.
Exit 0 only for cancelled and already_cancelled, the withdrawal an independent alternative may follow.
16 too_late: dispatch was already claimed; follow the original to its outcome. 17 already_finished: read
the original and do not repeat work. After an ambiguous failure (3), cancel again or read the request.`,
		Args: cobra.ExactArgs(1),
		RunE: a.actionCommand(func(ctx context.Context, client action_servicepb.ActionServiceClient, args []string) error {
			response, err := client.CancelActionRequest(ctx, &action_servicepb.CancelActionRequestRequest{RequestId: args[0]})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			var code int
			switch response.GetOutcome() {
			case action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_CANCELLED,
				action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_ALREADY_CANCELLED:
			case action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_TOO_LATE:
				code = exitTooLate
			case action_servicepb.CancellationOutcome_CANCELLATION_OUTCOME_ALREADY_FINISHED:
				code = exitAlreadyFinished
			default:
				return invalidResponse("unrecognized cancellation outcome %q", response.GetOutcome())
			}
			body, err := protoJSON(response)
			if err != nil {
				return err
			}
			if err := a.showOne(body, cancellationLine); err != nil {
				return err
			}
			return exitWith(code)
		}),
	}
}
