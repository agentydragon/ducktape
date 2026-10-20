package main

import (
	"cmp"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
)

const notificationsHelp = `Calls the Notification Service (AGENTPLANE_NOTIFICATIONS_URL, AGENTPLANE_NOTIFICATIONS_CREDENTIAL).

Subscriptions deliver notices to a Session inbox, so a long wait costs no turn. Reading and
acknowledging the inbox stays an explicit HTTP step. No command here has an outcome exit code; cancel
removes a subscription and never cancels an Action.`

var pullRequestPattern = regexp.MustCompile(`^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)$`)

func (a *app) notificationsCommand() *cobra.Command {
	return group("notifications", "Subscribe to events and manage subscriptions", notificationsHelp,
		group("subscribe", "Subscribe to a source", "Creates a subscription for this Session's inbox.",
			group("github", "Subscribe to GitHub activity", "Only pull requests (`pr`) are supported.", a.subscribePullRequestCommand())),
		a.sourcesCommand(), a.subscriptionsCommand(), a.subscriptionCommand(), a.renewCommand(), a.cancelSubscriptionCommand())
}

// destination is the inbox a subscription delivers to, the object AGENTPLANE_DESTINATION holds.
type destination struct {
	Ref struct {
		Namespace string `json:"namespace"`
		Name      string `json:"name"`
		UID       string `json:"uid"`
	} `json:"destination_ref"`
	SessionID string `json:"session_id"`
}

type eventFilter struct {
	Event   string   `json:"event"`
	Actions []string `json:"actions,omitempty"`
}

type githubSource struct {
	Provider   string `json:"provider"`
	Repository string `json:"repository"`
	Subject    struct {
		Kind   string `json:"kind"`
		Number int    `json:"number"`
	} `json:"subject"`
	Events []eventFilter `json:"events,omitempty"`
}

type subscribeRequest struct {
	destination
	IdempotencyKey string       `json:"idempotency_key"`
	Source         githubSource `json:"source"`
	LifetimeDays   *int         `json:"lifetime_days,omitempty"`
}

// optionalInt is an int flag that records whether it was given.
type optionalInt struct{ value *int }

func (o *optionalInt) Set(text string) error {
	number, err := strconv.Atoi(text)
	if err == nil {
		o.value = &number
	}
	return err
}

func (o *optionalInt) String() string {
	if o.value == nil {
		return ""
	}
	return strconv.Itoa(*o.value)
}

func (o *optionalInt) Type() string { return "int" }

// destination reads the inbox: --destination wins over AGENTPLANE_DESTINATION.
func (a *app) destination(flag string) (destination, error) {
	source, text := "--destination", flag
	if text == "" {
		source, text = "AGENTPLANE_DESTINATION", a.getenv("AGENTPLANE_DESTINATION")
	}
	if text == "" {
		return destination{}, usageFailure("no destination: set AGENTPLANE_DESTINATION or pass --destination JSON")
	}
	var parsed destination
	if err := json.Unmarshal([]byte(text), &parsed); err != nil {
		return destination{}, usageFailure("%s: %v", source, err)
	}
	if parsed.Ref.Namespace == "" || parsed.Ref.Name == "" || parsed.Ref.UID == "" || parsed.SessionID == "" {
		return destination{}, usageFailure("%s needs destination_ref.namespace, name and uid, and session_id", source)
	}
	return parsed, nil
}

// parseEvents reads NAME[:ACTION[,ACTION]] filters into their canonical order.
func parseEvents(specs []string) ([]eventFilter, error) {
	var filters []eventFilter
	for _, spec := range specs {
		name, actions, _ := strings.Cut(spec, ":")
		if name == "" {
			return nil, usageFailure("--event wants NAME[:ACTION[,ACTION]], got %q", spec)
		}
		filter := eventFilter{Event: name}
		if actions != "" {
			filter.Actions = strings.Split(actions, ",")
			slices.Sort(filter.Actions)
			filter.Actions = slices.Compact(filter.Actions)
		}
		filters = append(filters, filter)
	}
	slices.SortFunc(filters, func(x, y eventFilter) int {
		return cmp.Or(cmp.Compare(x.Event, y.Event), slices.Compare(x.Actions, y.Actions))
	})
	return slices.CompactFunc(filters, func(x, y eventFilter) bool {
		return x.Event == y.Event && slices.Equal(x.Actions, y.Actions)
	}), nil
}

// subscriptionKey derives the idempotency key from the canonical source, so repeating a command
// returns the same subscription.
func subscriptionKey(source githubSource) (string, error) {
	canonical, err := json.Marshal(source)
	if err != nil {
		return "", clientFailure(exitRefused, "INTERNAL", "idempotency key: %v", err)
	}
	sum := sha256.Sum256(canonical)
	return "agentplane-" + hex.EncodeToString(sum[:16]), nil
}

func (a *app) subscribePullRequestCommand() *cobra.Command {
	var events []string
	var key, destinationFlag string
	var lifetime optionalInt
	command := &cobra.Command{
		Use:   "pr OWNER/REPO#NUMBER [--event NAME[:ACTION[,ACTION]]]...",
		Short: "Follow a pull request",
		Long: `POST /v1/subscriptions for a GitHub pull request, delivering to the inbox named by
AGENTPLANE_DESTINATION ({"destination_ref": {...}, "session_id": "..."}) or --destination, which wins.
Subscribe first, then read the pull request's current state: this follows from creation, not from history.

--event replaces the default events (for example --event issue_comment:created) rather than adding to
them. The idempotency key is derived from the source, so repeating the command returns the same
subscription, and a transient failure (3) is retried by running it again; --key overrides the key.
Exit 4 when a changed --lifetime-days (1-30, default 7) meets the same key (renew changes the
lifetime), and when the key belongs to a cancelled subscription (pass a new --key).`,
		Args: cobra.ExactArgs(1),
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, args []string) error {
			match := pullRequestPattern.FindStringSubmatch(args[0])
			if match == nil {
				return usageFailure("want OWNER/REPO#NUMBER, got %q", args[0])
			}
			inbox, err := a.destination(destinationFlag)
			if err != nil {
				return err
			}
			body := subscribeRequest{destination: inbox, IdempotencyKey: key, LifetimeDays: lifetime.value}
			body.Source.Provider, body.Source.Repository = "github", match[1]+"/"+match[2]
			body.Source.Subject.Kind = "pull_request"
			if body.Source.Subject.Number, err = strconv.Atoi(match[3]); err != nil {
				return usageFailure("pull request number: %v", err)
			}
			if body.Source.Events, err = parseEvents(events); err != nil {
				return err
			}
			if body.IdempotencyKey == "" {
				if body.IdempotencyKey, err = subscriptionKey(body.Source); err != nil {
					return err
				}
			}
			response, err := svc.do(ctx, request{method: http.MethodPost, path: "/v1/subscriptions", body: body, conflict: "ALREADY_EXISTS"})
			if err != nil {
				return err
			}
			created, err := decode[struct {
				Cancelled bool `json:"cancelled"`
			}](response)
			if err != nil {
				return err
			}
			if created.Cancelled {
				return clientFailure(exitConflict, "ALREADY_EXISTS",
					"idempotency key %s belongs to a cancelled subscription: pass a new --key", body.IdempotencyKey)
			}
			return a.showOne(response, subscriptionLine)
		}),
	}
	command.Flags().StringArrayVar(&events, "event", nil, "NAME[:ACTION[,ACTION]]; replaces the default events; repeatable")
	command.Flags().Var(&lifetime, "lifetime-days", "subscription lifetime in days (service default 7)")
	command.Flags().StringVar(&key, "key", "", "idempotency key; derived from the source if absent")
	command.Flags().StringVar(&destinationFlag, "destination", "", "the destination JSON, instead of AGENTPLANE_DESTINATION")
	return command
}

func (a *app) sourcesCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "sources",
		Short: "List the providers this service can follow",
		Long: `GET /v1/sources: the enabled providers and what a notice from each contains. --json also has each
provider's subscription schema.`,
		Args: cobra.NoArgs,
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, _ []string) error {
			body, err := svc.fetch(ctx, "/v1/sources", nil)
			if err != nil {
				return err
			}
			return a.showOne(body, sourcesText)
		}),
	}
}

func (a *app) subscriptionsCommand() *cobra.Command {
	var limit int
	var after string
	command := &cobra.Command{
		Use:   "subscriptions",
		Short: "List this account's subscriptions",
		Long: `GET /v1/subscriptions, following server pages (128) until --limit items (default 50; 0 is unlimited).
To continue, pass the last item's id as --after.`,
		Args: cobra.NoArgs,
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, _ []string) error {
			items, err := collect(limit, after, func(after string, _ int) ([]json.RawMessage, error) {
				query := url.Values{}
				if after != "" {
					query.Set("after_id", after)
				}
				body, err := svc.fetch(ctx, "/v1/subscriptions", query)
				if err != nil {
					return nil, err
				}
				return decode[[]json.RawMessage](body)
			}, idOf)
			if err != nil {
				return err
			}
			return showPage(a, items, limit, idOf, subscriptionLine)
		}),
	}
	command.Flags().IntVar(&limit, "limit", defaultLimit, "stop after this many items; 0 is unlimited")
	command.Flags().StringVar(&after, "after", "", "continue after this subscription id")
	return command
}

func (a *app) subscriptionCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "subscription ID",
		Short: "Show one subscription",
		Long:  "GET /v1/subscriptions/{id}: its source, version, expiry and any source error. Exit 5 if it is not this account's.",
		Args:  cobra.ExactArgs(1),
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, args []string) error {
			body, err := svc.fetch(ctx, "/v1/subscriptions/"+url.PathEscape(args[0]), nil)
			if err != nil {
				return err
			}
			return a.showOne(body, subscriptionLine)
		}),
	}
}

func (a *app) renewCommand() *cobra.Command {
	var lifetime optionalInt
	command := &cobra.Command{
		Use:   "renew ID [--lifetime-days N]",
		Short: "Extend a subscription",
		Long: `GET /v1/subscriptions/{id}, then PATCH it with the version just read and the lifetime (1-30 days,
default 7). A version that changed in between exits 4: read the subscription and decide again.`,
		Args: cobra.ExactArgs(1),
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, args []string) error {
			path := "/v1/subscriptions/" + url.PathEscape(args[0])
			current, err := svc.fetch(ctx, path, nil)
			if err != nil {
				return err
			}
			read, err := decode[struct {
				Version int `json:"version"`
			}](current)
			if err != nil {
				return err
			}
			update := struct {
				Version      int  `json:"version"`
				LifetimeDays *int `json:"lifetime_days,omitempty"`
			}{read.Version, lifetime.value}
			body, err := svc.do(ctx, request{method: http.MethodPatch, path: path, body: update})
			if err != nil {
				return err
			}
			return a.showOne(body, subscriptionLine)
		}),
	}
	command.Flags().Var(&lifetime, "lifetime-days", "new lifetime in days (service default 7)")
	return command
}

func (a *app) cancelSubscriptionCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "cancel ID",
		Short: "Cancel a subscription",
		Long: `DELETE /v1/subscriptions/{id}. Idempotent. It does not cancel an Action, and accepted notices stay in
the inbox.`,
		Args: cobra.ExactArgs(1),
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, args []string) error {
			body, err := svc.do(ctx, request{method: http.MethodDelete, path: "/v1/subscriptions/" + url.PathEscape(args[0])})
			if err != nil {
				return err
			}
			return a.showOne(body, subscriptionLine)
		}),
	}
}
