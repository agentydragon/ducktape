package main

import (
	"cmp"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
)

const notificationsHelp = `Calls the Notification Service (AGENTPLANE_NOTIFICATIONS_URL, AGENTPLANE_NOTIFICATIONS_CREDENTIAL).

Subscriptions deliver notices to a Session inbox, so a long wait costs no turn. Reading and
acknowledging the inbox stays an explicit HTTP step. No command here has an outcome exit code; cancel
removes a subscription and never cancels an Action.`

const (
	githubEventPrefix  = "GITHUB_EVENT_NAME_"
	failureKindPrefix  = "SOURCE_FAILURE_KIND_"
	subscriptionsRoute = "/v1/subscriptions"
)

var pullRequestPattern = regexp.MustCompile(`^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)$`)

func (a *app) notificationsCommand() *cobra.Command {
	return group("notifications", "Subscribe to events and manage subscriptions", notificationsHelp,
		group("subscribe", "Subscribe to a source", "Creates a subscription for this Session's inbox.",
			group("github", "Subscribe to GitHub activity", "Only pull requests (`pr`) are supported.", a.subscribePullRequestCommand())),
		a.subscriptionsCommand(), a.subscriptionCommand(), a.renewCommand(), a.cancelSubscriptionCommand())
}

// optionalInt is an int32 flag that records whether it was given.
type optionalInt struct{ value *int32 }

func (o *optionalInt) Set(text string) error {
	number, err := strconv.ParseInt(text, 10, 32)
	if err == nil {
		o.value = proto.Int32(int32(number))
	}
	return err
}

func (o *optionalInt) String() string {
	if o.value == nil {
		return ""
	}
	return strconv.Itoa(int(*o.value))
}

func (o *optionalInt) Type() string { return "int" }

// decodeMessage reads a response into its generated message. Fields a newer service added are skipped.
func decodeMessage(raw []byte, into proto.Message) error {
	if err := (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(raw, into); err != nil {
		return invalidResponse("%v", err)
	}
	return nil
}

// responseField is the value of one top-level field of a response, unmodified.
func responseField(raw []byte, name string) (json.RawMessage, error) {
	fields, err := decode[map[string]json.RawMessage](raw)
	if err != nil {
		return nil, err
	}
	value, ok := fields[name]
	if !ok {
		return nil, invalidResponse("no %q in the response", name)
	}
	return value, nil
}

// destination reads the inbox: --destination wins over AGENTPLANE_DESTINATION. Both hold the
// destination fields of a CreateSubscriptionRequest, `{"destination_ref": {...}, "session_id": "..."}`.
func (a *app) destination(flag string) (*notificationspb.CreateSubscriptionRequest, error) {
	source, text := "--destination", flag
	if text == "" {
		source, text = "AGENTPLANE_DESTINATION", a.getenv("AGENTPLANE_DESTINATION")
	}
	if text == "" {
		return nil, usageFailure("no destination: set AGENTPLANE_DESTINATION or pass --destination JSON")
	}
	var parsed notificationspb.CreateSubscriptionRequest
	if err := protojson.Unmarshal([]byte(text), &parsed); err != nil {
		return nil, usageFailure("%s: %v", source, err)
	}
	ref := parsed.GetDestinationRef()
	if ref.GetNamespace() == "" || ref.GetName() == "" || ref.GetUid() == "" || parsed.GetSessionId() == "" {
		return nil, usageFailure("%s needs destination_ref.namespace, name and uid, and session_id", source)
	}
	destination := &notificationspb.CreateSubscriptionRequest{DestinationRef: ref, SessionId: parsed.GetSessionId()}
	if !proto.Equal(&parsed, destination) {
		return nil, usageFailure("%s holds only destination_ref and session_id", source)
	}
	return destination, nil
}

// parseEvents reads NAME[:ACTION[,ACTION]] filters into their canonical order.
func parseEvents(specs []string) ([]*notificationspb.GitHubEventFilter, error) {
	var filters []*notificationspb.GitHubEventFilter
	for _, spec := range specs {
		name, actions, _ := strings.Cut(spec, ":")
		event := notificationspb.GitHubEventName(notificationspb.GitHubEventName_value[githubEventPrefix+strings.ToUpper(name)])
		if name == "" || event == notificationspb.GitHubEventName_GITHUB_EVENT_NAME_UNSPECIFIED {
			return nil, usageFailure("--event wants NAME[:ACTION[,ACTION]] with a GitHub event name such as issue_comment, got %q", spec)
		}
		filter := &notificationspb.GitHubEventFilter{Event: event}
		if actions != "" {
			filter.Actions = strings.Split(actions, ",")
			slices.Sort(filter.Actions)
			filter.Actions = slices.Compact(filter.Actions)
		}
		filters = append(filters, filter)
	}
	slices.SortFunc(filters, func(x, y *notificationspb.GitHubEventFilter) int {
		return cmp.Or(cmp.Compare(x.Event, y.Event), slices.Compare(x.Actions, y.Actions))
	})
	return slices.CompactFunc(filters, func(x, y *notificationspb.GitHubEventFilter) bool {
		return x.Event == y.Event && slices.Equal(x.Actions, y.Actions)
	}), nil
}

// subscriptionKey derives the idempotency key from the source, so repeating a command returns the same
// subscription. The events are in canonical order, so their order and repeats do not change it.
func subscriptionKey(repository string, number int32, events []*notificationspb.GitHubEventFilter) string {
	canonical := fmt.Sprintf("github %s pull_request %d", repository, number)
	for _, filter := range events {
		canonical += fmt.Sprintf(" %s:%s", filter.Event, strings.Join(filter.Actions, ","))
	}
	sum := sha256.Sum256([]byte(canonical))
	return "agentplane-" + hex.EncodeToString(sum[:16])
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
			number, err := strconv.ParseInt(match[3], 10, 32)
			if err != nil {
				return usageFailure("pull request number: %v", err)
			}
			filters, err := parseEvents(events)
			if err != nil {
				return err
			}
			body, err := a.destination(destinationFlag)
			if err != nil {
				return err
			}
			repository := match[1] + "/" + match[2]
			body.IdempotencyKey = key
			if body.IdempotencyKey == "" {
				body.IdempotencyKey = subscriptionKey(repository, int32(number), filters)
			}
			body.LifetimeDays = lifetime.value
			body.Source = &notificationspb.Source{Provider: &notificationspb.Source_Github{Github: &notificationspb.GitHubSource{
				Repository: repository,
				Subject: &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_PullRequest{
					PullRequest: &notificationspb.PullRequestSubject{Number: int32(number)},
				}},
				Events: filters,
			}}}
			response, err := svc.do(ctx, request{method: http.MethodPost, path: subscriptionsRoute, body: body})
			if err != nil {
				return err
			}
			var created notificationspb.CreateSubscriptionResponse
			if err := decodeMessage(response, &created); err != nil {
				return err
			}
			if created.GetSubscription().GetCancelled() {
				return clientFailure(exitConflict, "ALREADY_EXISTS",
					"idempotency key %s belongs to a cancelled subscription: pass a new --key", body.IdempotencyKey)
			}
			return a.showSubscription(response)
		}),
	}
	command.Flags().StringArrayVar(&events, "event", nil, "NAME[:ACTION[,ACTION]]; replaces the default events; repeatable")
	command.Flags().Var(&lifetime, "lifetime-days", "subscription lifetime in days (service default 7)")
	command.Flags().StringVar(&key, "key", "", "idempotency key; derived from the source if absent")
	command.Flags().StringVar(&destinationFlag, "destination", "", "the destination JSON, instead of AGENTPLANE_DESTINATION")
	return command
}

// showSubscription prints the subscription of a single-subscription response.
func (a *app) showSubscription(response []byte) error {
	subscription, err := responseField(response, "subscription")
	if err != nil {
		return err
	}
	return a.showOne(subscription, subscriptionLine)
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
					query.Set("afterId", after)
				}
				body, err := svc.fetch(ctx, subscriptionsRoute, query)
				if err != nil {
					return nil, err
				}
				page, err := responseField(body, "subscriptions")
				if err != nil {
					return nil, err
				}
				return decode[[]json.RawMessage](page)
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
			body, err := svc.fetch(ctx, subscriptionsRoute+"/"+url.PathEscape(args[0]), nil)
			if err != nil {
				return err
			}
			return a.showSubscription(body)
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
			path := subscriptionsRoute + "/" + url.PathEscape(args[0])
			current, err := svc.fetch(ctx, path, nil)
			if err != nil {
				return err
			}
			var read notificationspb.GetSubscriptionResponse
			if err := decodeMessage(current, &read); err != nil {
				return err
			}
			update := &notificationspb.RenewSubscriptionRequest{Version: read.GetSubscription().GetVersion(), LifetimeDays: lifetime.value}
			body, err := svc.do(ctx, request{method: http.MethodPatch, path: path, body: update})
			if err != nil {
				return err
			}
			return a.showSubscription(body)
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
			body, err := svc.do(ctx, request{method: http.MethodDelete, path: subscriptionsRoute + "/" + url.PathEscape(args[0])})
			if err != nil {
				return err
			}
			return a.showSubscription(body)
		}),
	}
}

// enumSuffix is a proto enum value name without its prefix, lowercased: `SOURCE_FAILURE_KIND_RATE_LIMITED`
// with prefix `SOURCE_FAILURE_KIND_` reads `rate_limited`.
func enumSuffix(name, prefix string) string {
	return strings.ToLower(strings.TrimPrefix(name, prefix))
}

func subscriptionLine(raw json.RawMessage) (string, error) {
	var subscription notificationspb.Subscription
	if err := decodeMessage(raw, &subscription); err != nil {
		return "", err
	}
	line := fields(subscription.Id, sourceLabel(subscription.Source), "v"+strconv.FormatInt(subscription.Version, 10))
	if subscription.ExpiresAt != nil {
		line = fields(line, "expires "+subscription.ExpiresAt.AsTime().UTC().Format(time.RFC3339))
	}
	if subscription.Cancelled {
		line += "  cancelled"
	}
	if subscription.ErrorKind != nil {
		line += "  error " + enumSuffix(subscription.ErrorKind.String(), failureKindPrefix)
	}
	return line, nil
}

func sourceLabel(source *notificationspb.Source) string {
	switch provider := source.GetProvider().(type) {
	case *notificationspb.Source_Actions:
		return "actions " + provider.Actions.GetRequestId()
	case *notificationspb.Source_Github:
		return fmt.Sprintf("github %s %s", provider.Github.GetRepository(), subjectLabel(provider.Github.GetSubject()))
	default:
		return "unknown source"
	}
}

func subjectLabel(subject *notificationspb.GitHubSubject) string {
	switch kind := subject.GetSubject().(type) {
	case *notificationspb.GitHubSubject_PullRequest:
		return fmt.Sprintf("pull_request:%d", kind.PullRequest.GetNumber())
	case *notificationspb.GitHubSubject_Issue:
		return fmt.Sprintf("issue:%d", kind.Issue.GetNumber())
	case *notificationspb.GitHubSubject_Branch:
		return "branch:" + kind.Branch.GetName()
	case *notificationspb.GitHubSubject_Commit:
		return "commit:" + kind.Commit.GetSha()
	default:
		return "unknown subject"
	}
}
