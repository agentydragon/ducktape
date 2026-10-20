package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"
)

// recoveryTimeout bounds the one lookup that follows a submit whose response was lost. It is its
// own budget because the deadline may be what lost the response.
const recoveryTimeout = 10 * time.Second

const actionsHelp = `Calls the Action Service (AGENTPLANE_ACTIONS_URL, AGENTPLANE_ACTIONS_CREDENTIAL).

Discover with groups, list, show and policy; submit an Action and follow it with submit --wait, wait,
result, get and events; withdraw it with cancel. Approval is not success: wait and result exit 0 only
for a succeeded Action, 11 denied, 12 failed, 13 cancelled, 14 execution_unknown, 10 when --timeout
ran out first. Whatever a succeeded Action's result says, the exit status is 0.`

func (a *app) actionsCommand() *cobra.Command {
	return group("actions", "Discover, submit, follow and cancel Actions", actionsHelp,
		a.groupsCommand(), a.listCommand(), a.showCommand(), a.policyCommand(), a.submitCommand(),
		a.getCommand(), a.resultCommand(), a.waitCommand(), a.requestsCommand(), a.eventsCommand(), a.cancelCommand())
}

func (s *service) fetch(ctx context.Context, path string, query url.Values) ([]byte, error) {
	return s.do(ctx, request{method: http.MethodGet, path: path, query: query})
}

func (a *app) catalog(ctx context.Context, svc *service) ([]json.RawMessage, error) {
	body, err := svc.fetch(ctx, "/v1/action-groups", nil)
	if err != nil {
		return nil, err
	}
	return decode[[]json.RawMessage](body)
}

func (a *app) groupsCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "groups",
		Short: "List ActionGroups",
		Long: `GET /v1/action-groups. --json prints one ActionGroup per line, each with every Action's description
and input schema, which is large; use list or show for one Action.`,
		Args: cobra.NoArgs,
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, _ []string) error {
			groups, err := a.catalog(ctx, svc)
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
		Long:  "GET /v1/action-groups, flattened to one Action per line, of GROUP if given. An unknown GROUP exits 5.",
		Args:  cobra.MaximumNArgs(1),
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, args []string) error {
			groups, err := a.catalog(ctx, svc)
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
		Long:  "GET /v1/action-groups/{group}/actions/{action}: the Action's description and its input schema.",
		Args:  cobra.ExactArgs(1),
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, args []string) error {
			group, name, err := splitAction(args[0])
			if err != nil {
				return err
			}
			body, err := svc.fetch(ctx, "/v1/action-groups/"+url.PathEscape(group)+"/actions/"+url.PathEscape(name), nil)
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
		Long:  "GET /v1/action-policy: the bindings and auto-approve entries that apply to the calling ServiceAccount (`subject`).",
		Args:  cobra.NoArgs,
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, _ []string) error {
			body, err := svc.fetch(ctx, "/v1/action-policy", nil)
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

// reader reads the target's receipt. A key is resolved to an ID by the first read.
func (t target) reader(svc *service, until waitUntil) reader {
	id := t.id
	return func(ctx context.Context, wait float64) (*receipt, error) {
		if id != "" {
			return readReceipt(ctx, svc, id, until, wait)
		}
		found, err := lookupByKey(ctx, svc, t.key)
		if err == nil {
			id = found.ID
		}
		return found, err
	}
}

func (a *app) printReceipt(r *receipt) error {
	return a.showOne(r.raw, receiptLine)
}

func (a *app) getCommand() *cobra.Command {
	var key string
	command := &cobra.Command{
		Use:   "get {ID | --key KEY}",
		Short: "Read an Action request",
		Long: `GET /v1/action-requests/{id}, or GET /v1/action-requests?idempotency_key=KEY. Prints the receipt and
exits 0 whatever its state; use wait or result to follow it. Exit 5 if there is no such request.`,
		Args: cobra.MaximumNArgs(1),
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, args []string) error {
			t, err := newTarget(args, key)
			if err != nil {
				return err
			}
			r, err := t.reader(svc, untilTerminal)(ctx, 0)
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
		Long: `Reads GET /v1/action-requests/{id} with a server-side wait, re-issued within --timeout (default 30s;
0 reads once). Prints the receipt. --until terminal (the default) waits for succeeded, failed, denied,
cancelled or execution_unknown; decision waits for the state to leave decision_pending.

Exit 0 only if the condition holds and the Action did not fail: 11 denied, 12 failed, 13 cancelled,
14 execution_unknown, 10 if --timeout ran out first. If the service is unreachable when it does, exit 3
with the last receipt read on stdout. Ending the wait never cancels the Action.`,
		Args: cobra.MaximumNArgs(1),
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, args []string) error {
			t, err := newTarget(args, key)
			if err != nil {
				return err
			}
			condition, err := parseUntil(until)
			if err != nil {
				return err
			}
			last, err := await(ctx, condition, nil, t.reader(svc, condition))
			if err != nil {
				return a.afterWait(last, err)
			}
			return a.finish(last, condition)
		}),
	}
	command.Flags().StringVar(&key, "key", "", "the idempotency key the request was submitted under")
	command.Flags().StringVar(&until, "until", string(untilTerminal), "decision or terminal")
	return command
}

func (a *app) resultCommand() *cobra.Command {
	var key string
	command := &cobra.Command{
		Use:   "result {ID | --key KEY}",
		Short: "Wait for an Action to finish and print its result",
		Long: `Waits as wait does (within --timeout), then, for a succeeded Action, prints its stored
execution.result: the MCP tool's CallToolResult, or the sandbox executor's JSON. Text mode prints the
result's text blocks, or indented JSON when there are none. For any other state it prints the
receipt, so the reason (a Decision note, execution.error) is visible.

Exit codes are wait's. A succeeded Action exits 0 whatever its result says: read the result to
learn a script's own exit code or an MCP isError.`,
		Args: cobra.MaximumNArgs(1),
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, args []string) error {
			t, err := newTarget(args, key)
			if err != nil {
				return err
			}
			last, err := await(ctx, untilTerminal, nil, t.reader(svc, untilTerminal))
			if err != nil {
				return a.afterWait(last, err)
			}
			if last.State == "succeeded" {
				return a.printResult(last)
			}
			return a.finish(last, untilTerminal)
		}),
	}
	command.Flags().StringVar(&key, "key", "", "the idempotency key the request was submitted under")
	return command
}

func (a *app) printResult(r *receipt) error {
	result := json.RawMessage("null")
	if r.Execution != nil && len(r.Execution.Result) > 0 {
		result = r.Execution.Result
	}
	return a.showOne(result, resultText)
}

type submission struct {
	IdempotencyKey string `json:"idempotency_key"`
	Action         struct {
		Group string `json:"group"`
		Name  string `json:"name"`
	} `json:"action"`
	Arguments   json.RawMessage `json:"arguments"`
	Title       string          `json:"title"`
	Description *string         `json:"description,omitempty"`
}

func (a *app) submitCommand() *cobra.Command {
	var key, title, description, arguments, until string
	var wait bool
	command := &cobra.Command{
		Use:   "submit GROUP/ACTION --title TEXT [--arguments JSON|@FILE|-] [--wait [--until decision|terminal]]",
		Short: "Submit an Action request",
		Long: `POST /v1/action-requests. --arguments is sent as given (default {}) and validated by the service
against the Action's current schema; read it first with show. --title is one line for the operator
(60 characters); --description adds detail.

Without --key a random idempotency key is generated and written to stderr before the POST. A repeated
key is refused (exit 4), so the key is the only handle on a request whose response is lost: retry a
failed submit with --key KEY, never without it, and recover with "get --key KEY". The CLI sends one
POST per key. If the response is lost it looks the key up once: found, it carries on as if the response
had arrived; not found, it exits 15 (never resubmit; look again later).

Without --wait it prints the receipt and exits 0 once the request is recorded, whatever its state.
--wait then follows wait, with its options and exit codes.`,
		Args: cobra.ExactArgs(1),
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, args []string) error {
			group, name, err := splitAction(args[0])
			if err != nil {
				return err
			}
			condition, err := parseUntil(until)
			if err != nil {
				return err
			}
			if !wait && until != string(untilTerminal) {
				return usageFailure("--until needs --wait")
			}
			body := submission{Title: title, IdempotencyKey: key}
			body.Action.Group, body.Action.Name = group, name
			if body.Arguments, err = a.readArguments(arguments); err != nil {
				return err
			}
			if description != "" {
				body.Description = &description
			}
			if body.IdempotencyKey == "" {
				body.IdempotencyKey = uuid.NewString()
				a.announceKey(body.IdempotencyKey)
			}
			created, err := a.post(ctx, svc, body)
			if err != nil {
				return err
			}
			if !wait {
				return a.printReceipt(created)
			}
			last, err := await(ctx, condition, created, func(ctx context.Context, seconds float64) (*receipt, error) {
				return readReceipt(ctx, svc, created.ID, condition, seconds)
			})
			if err != nil {
				return a.afterWait(last, err)
			}
			return a.finish(last, condition)
		}),
	}
	command.Flags().StringVar(&key, "key", "", "idempotency key; generated and printed on stderr if absent")
	command.Flags().StringVar(&title, "title", "", "one line saying what the request does, for the operator (required)")
	command.Flags().StringVar(&description, "description", "", "optional detail the title leaves out")
	command.Flags().StringVar(&arguments, "arguments", "", "the Action's arguments: JSON, @FILE or - for stdin")
	command.Flags().BoolVar(&wait, "wait", false, "then wait for the outcome, as the wait command")
	command.Flags().StringVar(&until, "until", string(untilTerminal), "with --wait: decision or terminal")
	_ = command.MarkFlagRequired("title")
	return command
}

func (a *app) readArguments(spec string) (json.RawMessage, error) {
	var data []byte
	var err error
	switch {
	case spec == "":
		return json.RawMessage("{}"), nil
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
	if !json.Valid(data) {
		return nil, usageFailure("--arguments is not valid JSON")
	}
	return data, nil
}

func (a *app) announceKey(key string) {
	if a.asJSON {
		fmt.Fprintf(a.stderr, "{\"idempotency_key\": %q}\n", key)
	} else {
		fmt.Fprintf(a.stderr, "idempotency key: %s\n", key)
	}
}

// post makes the one POST for a submission. A failure that may have committed it is followed by a
// single lookup under the same key.
func (a *app) post(ctx context.Context, svc *service, body submission) (*receipt, error) {
	response, err := svc.do(ctx, request{method: http.MethodPost, path: "/v1/action-requests", body: body, conflict: "ALREADY_EXISTS"})
	if err == nil {
		created, parseErr := parseReceipt(response)
		if parseErr == nil {
			return created, nil
		}
		// An answer that cannot be read is as good as a lost one.
		unreadable := clientFailure(exitRefused, "INTERNAL", "%v", parseErr)
		unreadable.sent = true
		err = unreadable
	}
	var failed *failure
	if !errors.As(err, &failed) {
		return nil, err
	}
	switch {
	case failed.exit == exitConflict:
		failed.Message = fmt.Sprintf("%s; idempotency key %s is in use: read that request with `agentplane actions get --key %s`",
			failed.Message, body.IdempotencyKey, body.IdempotencyKey)
	case failed.ambiguous():
		lookup, cancel := context.WithTimeout(a.ctx, recoveryTimeout)
		defer cancel()
		found, lookupErr := lookupByKey(lookup, svc, body.IdempotencyKey)
		if errors.Is(lookupErr, errInterrupted) {
			return nil, lookupErr
		}
		if lookupErr == nil {
			return found, nil
		}
		failed.exit = exitSubmitLost
		failed.Message = fmt.Sprintf("%s; a lookup by the key found no request (%v): look again later with `agentplane actions get --key %s`, and do not resubmit",
			failed.Message, lookupErr, body.IdempotencyKey)
	}
	return nil, failed
}

func (a *app) requestsCommand() *cobra.Command {
	var states []string
	var limit int
	var after string
	command := &cobra.Command{
		Use:   "requests [--state STATE]...",
		Short: "List this account's Action requests, newest first",
		Long: `GET /v1/action-requests, following server pages (128 at most) until --limit items (default 50; 0 is
unlimited). To continue, pass the last item's id as --after.`,
		Args: cobra.NoArgs,
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, _ []string) error {
			items, err := collect(limit, after, func(after string, size int) ([]json.RawMessage, error) {
				query := url.Values{"limit": {strconv.Itoa(size)}, "state": states}
				if after != "" {
					query.Set("after_id", after)
				}
				body, err := svc.fetch(ctx, "/v1/action-requests", query)
				if err != nil {
					return nil, err
				}
				return decode[[]json.RawMessage](body)
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
		Long: `GET /v1/action-requests/{id}/events, following server pages (128 at most) until --limit items (default
50; 0 is unlimited). To continue, pass the last item's sequence as --after.`,
		Args: cobra.ExactArgs(1),
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, args []string) error {
			items, err := collect(limit, after, func(after, size int) ([]json.RawMessage, error) {
				query := url.Values{"limit": {strconv.Itoa(size)}, "after_sequence": {strconv.Itoa(after)}}
				body, err := svc.fetch(ctx, "/v1/action-requests/"+url.PathEscape(args[0])+"/events", query)
				if err != nil {
					return nil, err
				}
				return decode[[]json.RawMessage](body)
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
		Long: `POST /v1/action-requests/{id}/cancel. Prints the service's result ({"outcome", "request"}).
Exit 0 only for cancelled and already_cancelled, the withdrawal an independent alternative may follow.
16 too_late: dispatch was already claimed; follow the original to its outcome. 17 already_finished: read
the original and do not repeat work. After an ambiguous failure (3), cancel again or read the request.`,
		Args: cobra.ExactArgs(1),
		RunE: a.command("ACTIONS", func(ctx context.Context, svc *service, args []string) error {
			body, err := svc.do(ctx, request{method: http.MethodPost, path: "/v1/action-requests/" + url.PathEscape(args[0]) + "/cancel"})
			if err != nil {
				return err
			}
			result, err := decode[struct {
				Outcome string `json:"outcome"`
			}](body)
			if err != nil {
				return err
			}
			var code int
			switch result.Outcome {
			case "cancelled", "already_cancelled":
			case "too_late":
				code = exitTooLate
			case "already_finished":
				code = exitAlreadyFinished
			default:
				return invalidResponse("unrecognized cancellation outcome %q", result.Outcome)
			}
			if err := a.showOne(body, cancellationLine); err != nil {
				return err
			}
			return exitWith(code)
		}),
	}
}
