package notifications

import (
	"cmp"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	notificationdocs "github.com/agentydragon/ducktape/agentplane/notification_service/docs"
	"github.com/google/uuid"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

const notificationsHelp = `Calls the Notification Service (AGENTPLANE_NOTIFICATIONS_GRPC_URL; optional AGENTPLANE_NOTIFICATIONS_GRPC_CREDENTIAL bearer).

Subscriptions deliver notices to a Session inbox, so a long wait costs no turn; send puts a message in
another Session's inbox. Inbox reads are non-destructive; acknowledgement advances the handled prefix.
No command here reports an Action outcome; cancel removes a subscription and never cancels an Action. Inbox retirement
is irreversible and stops future messages and delivery.`

const (
	githubEventPrefix = "GITHUB_EVENT_NAME_"
	failureKindPrefix = "SOURCE_FAILURE_KIND_"
)

func (a *commands) notificationCall(fn func(context.Context, notificationspb.NotificationsClient, []string) error) func(*cobra.Command, []string) error {
	return func(_ *cobra.Command, args []string) error {
		conn, err := a.ConnectGRPC("NOTIFICATIONS_GRPC")
		if err != nil {
			return err
		}
		defer conn.Close()
		ctx, cancel := a.Deadline()
		defer cancel()
		return fn(ctx, notificationspb.NewNotificationsClient(conn), args)
	}
}

var notificationJSONOptions = protojson.MarshalOptions{EmitUnpopulated: true}

var pullRequestPattern = regexp.MustCompile(`^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)$`)

func (a *commands) notificationsCommand() *cobra.Command {
	return invocation.Group("notifications", "Follow events, subscriptions and inboxes", notificationsHelp,
		&cobra.Command{
			Use:   "guide [subscriptions|inbox|github|messages]",
			Short: "Read a Notification workflow guide",
			Args:  cobra.MaximumNArgs(1),
			RunE: func(_ *cobra.Command, args []string) error {
				name := "llms.txt"
				if len(args) == 1 {
					guides := map[string]string{"subscriptions": "subscriptions.md", "inbox": "inbox_handling.md", "github": "github.md", "messages": "messages.md"}
					var ok bool
					if name, ok = guides[args[0]]; !ok {
						return invocation.UsageFailure("unknown notifications guide %q", args[0])
					}
				}
				content, err := notificationdocs.Agents.ReadFile("agents/" + name)
				if err != nil {
					return err
				}
				_, err = fmt.Fprint(a.Stdout(), string(content))
				return err
			},
		},
		invocation.Group("subscribe", "Subscribe to a source", "Creates a subscription for this Session's inbox.",
			a.subscribeRequestCommand(),
			invocation.Group("github", "Subscribe to GitHub activity", "Only pull requests (`pr`) have a dedicated command.", a.subscribePullRequestCommand())),
		a.inboxCommand(), a.inboxesCommand(), a.subscriptionsCommand(), a.subscriptionCommand(), a.renewCommand(), a.cancelSubscriptionCommand(),
		a.sendCommand(), a.messageCommand())
}

func (a *commands) subscribeRequestCommand() *cobra.Command {
	var filename string
	command := &cobra.Command{
		Use:   "create --request-file FILE",
		Short: "Create a subscription from a protobuf JSON request",
		Long:  "Read a CreateSubscriptionRequest as protobuf JSON from FILE, or - for stdin. Supply sessionId, idempotencyKey and source; retry with the identical request.",
		Args:  cobra.NoArgs,
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, _ []string) error {
			var data []byte
			var err error
			if filename == "-" {
				data, err = io.ReadAll(a.Stdin())
			} else {
				data, err = os.ReadFile(filename)
			}
			if err != nil {
				return invocation.UsageFailure("--request-file: %v", err)
			}
			var request notificationspb.CreateSubscriptionRequest
			if err := protojson.Unmarshal(data, &request); err != nil {
				return invocation.UsageFailure("--request-file: %v", err)
			}
			response, err := svc.CreateSubscription(ctx, &request)
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response.GetSubscription().GetCancelled() {
				return invocation.ClientFailure(invocation.ExitConflict, "ALREADY_EXISTS", "idempotency key %s belongs to a cancelled subscription: use a new key", request.GetIdempotencyKey())
			}
			return a.showSubscription(response.GetSubscription())
		}),
	}
	command.Flags().StringVar(&filename, "request-file", "", "protobuf JSON request file; - reads stdin")
	_ = command.MarkFlagRequired("request-file")
	return command
}

func (a *commands) inboxCommand() *cobra.Command {
	return invocation.Group("inbox", "Inspect, read and manage a Session inbox", "Reading is non-destructive; acknowledgement advances the handled prefix.",
		a.getInboxCommand(), a.readInboxCommand(), a.ackInboxCommand(), a.retireInboxCommand())
}

func (a *commands) inboxesCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "inboxes",
		Short: "List this account's Session inboxes",
		Long:  "Notifications.ListInboxes. Lists inboxes opened for this account, including retired inboxes that have not been purged.",
		Args:  cobra.NoArgs,
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, _ []string) error {
			response, err := svc.ListInboxes(ctx, &notificationspb.ListInboxesRequest{})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			return invocation.ShowProto(a.Invocation, response.GetInboxes(), inboxLine, notificationJSONOptions)
		}),
	}
}

func (a *commands) getInboxCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "get SESSION_ID",
		Short: "Show one Session inbox",
		Long:  "Notifications.GetInbox. Reports the current state of the inbox addressed by Session ID.",
		Args:  cobra.ExactArgs(1),
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			response, err := svc.GetInbox(ctx, &notificationspb.GetInboxRequest{SessionId: args[0]})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response.GetInbox() == nil {
				return a.ShowOne([]byte("null"), func(json.RawMessage) (string, error) {
					return "no inbox for session " + args[0], nil
				})
			}
			return invocation.ShowProto(a.Invocation, []*notificationspb.Inbox{response.GetInbox()}, inboxLine, notificationJSONOptions)
		}),
	}
}

func (a *commands) retireInboxCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "retire SESSION_ID",
		Short: "Retire a Session inbox",
		Long: `Notifications.RetireInbox. Irreversibly stops future matching, messages and delivery to the inbox and cancels its
subscriptions. It does not cancel Actions or withdraw commands already submitted to the runner. Use only when the
Session no longer needs this inbox.`,
		Args: cobra.ExactArgs(1),
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			response, err := svc.RetireInbox(ctx, &notificationspb.RetireInboxRequest{SessionId: args[0]})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			return invocation.ShowProto(a.Invocation, []*notificationspb.RetireInboxResponse{response}, func(*notificationspb.RetireInboxResponse) (string, error) {
				return "retired inbox for session " + args[0], nil
			}, notificationJSONOptions)
		}),
	}
}

func (a *commands) readInboxCommand() *cobra.Command {
	var after int64
	var limit int
	command := &cobra.Command{
		Use:   "read SESSION_ID [--after CURSOR] [--limit N]",
		Short: "Read one page of inbox entries",
		Long: `Notifications.ReadInbox. Reads are non-destructive and return one contiguous page.
Continue after the last returned entry's cursor. Text output summarizes the entries; --json prints the
full response, including provider payloads and the latest notice state. The service returns at most 128
entries per page, and expired payloads are reported in inbox.expiredThrough.`,
		Args: cobra.ExactArgs(1),
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			if after < 0 {
				return invocation.UsageFailure("--after must not be negative")
			}
			if limit < 1 || limit > invocation.MaxPage {
				return invocation.UsageFailure("--limit must be between 1 and %d", invocation.MaxPage)
			}
			response, err := svc.ReadInbox(ctx, &notificationspb.ReadInboxRequest{SessionId: args[0], AfterCursor: after, Limit: proto.Int32(int32(limit))})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			return invocation.ShowProto(a.Invocation, []*notificationspb.ReadInboxResponse{response}, inboxPageLine, notificationJSONOptions)
		}),
	}
	command.Flags().Int64Var(&after, "after", 0, "read entries with cursors greater than CURSOR")
	command.Flags().IntVar(&limit, "limit", invocation.MaxPage, "maximum entries in this page (1-128)")
	return command
}

func (a *commands) ackInboxCommand() *cobra.Command {
	var through int64
	command := &cobra.Command{
		Use:   "ack SESSION_ID --through CURSOR",
		Short: "Acknowledge a handled inbox prefix",
		Long: `Notifications.AcknowledgeInbox. Acknowledgement covers every entry through the cursor,
across all subscriptions in this inbox. Read and handle the whole contiguous prefix first. The service
never moves the cursor backwards and refuses a cursor beyond the inbox's last entry.`,
		Args: cobra.ExactArgs(1),
		PreRunE: func(cmd *cobra.Command, _ []string) error {
			if !cmd.Flags().Changed("through") {
				return invocation.UsageFailure("--through is required")
			}
			if through < 0 {
				return invocation.UsageFailure("--through must not be negative")
			}
			return nil
		},
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			response, err := svc.AcknowledgeInbox(ctx, &notificationspb.AcknowledgeInboxRequest{SessionId: args[0], ThroughCursor: through})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			return invocation.ShowProto(a.Invocation, []*notificationspb.Inbox{response.GetInbox()}, inboxLine, notificationJSONOptions)
		}),
	}
	command.Flags().Int64Var(&through, "through", 0, "last cursor you have handled; required")
	return command
}

func inboxPageLine(page *notificationspb.ReadInboxResponse) (string, error) {
	if page == nil || page.GetInbox().GetId() == "" {
		return "", invocation.InvalidResponse("no inbox in the response")
	}
	lines := []string{inboxSummary(page.GetInbox())}
	if page.GetNotice() != nil {
		lines = append(lines, noticeLine(page.GetNotice()))
	}
	if len(page.GetEntries()) == 0 {
		lines = append(lines, "no entries in this page")
	}
	for _, entry := range page.GetEntries() {
		lines = append(lines, inboxEntryLine(entry))
	}
	return strings.Join(lines, "\n"), nil
}

func inboxLine(inbox *notificationspb.Inbox) (string, error) {
	if inbox == nil || inbox.GetId() == "" {
		return "", invocation.InvalidResponse("no inbox ID in the response")
	}
	return inboxSummary(inbox), nil
}

func inboxSummary(inbox *notificationspb.Inbox) string {
	line := invocation.Fields(
		"inbox", inbox.GetId(),
		"acknowledged", strconv.FormatInt(inbox.GetAcknowledged(), 10),
		"covered", strconv.FormatInt(inbox.GetCovered(), 10),
		"last", strconv.FormatInt(inbox.GetLastCursor(), 10),
		"expired through", strconv.FormatInt(inbox.GetExpiredThrough(), 10),
	)
	if inbox.GetRetired() {
		line += "  retired"
	}
	if inbox.GetDeliveryError() != "" {
		line += invocation.Fields("delivery error", strconv.Quote(inbox.GetDeliveryError()))
	}
	return line
}

func noticeLine(notice *notificationspb.Notice) string {
	line := invocation.Fields(
		"notice through", strconv.FormatInt(notice.GetThroughCursor(), 10),
		"attempted", strconv.FormatBool(notice.GetAttempted()),
		"admitted", strconv.FormatBool(notice.GetAdmitted()),
		"confirmed", strconv.FormatBool(notice.GetConfirmed()),
	)
	if notice.GetError() != "" {
		line += invocation.Fields("error", strconv.Quote(notice.GetError()))
	}
	return line
}

func inboxEntryLine(entry *notificationspb.InboxEntry) string {
	var identity string
	switch provider := entry.GetEvent().GetProvider().(type) {
	case *notificationspb.EventIdentity_Actions:
		identity = fmt.Sprintf("actions %s sequence %d", provider.Actions.GetRequestId(), provider.Actions.GetSequence())
	case *notificationspb.EventIdentity_Github:
		identity = "github " + invocation.EnumSuffix(provider.Github.GetEvent().String(), githubEventPrefix)
		if action := provider.Github.GetAction(); action != "" {
			identity += " " + strconv.Quote(action)
		}
	case *notificationspb.EventIdentity_GithubObservation:
		identity = fmt.Sprintf("github %s observation sequence %d",
			invocation.EnumSuffix(provider.GithubObservation.GetEvent().String(), githubEventPrefix),
			provider.GithubObservation.GetSequence())
	case *notificationspb.EventIdentity_GithubMergeability:
		switch observation := provider.GithubMergeability.GetObservation().(type) {
		case *notificationspb.GitHubMergeabilityEvent_Sequence:
			identity = fmt.Sprintf("github PR #%d mergeability sequence %d",
				provider.GithubMergeability.GetPullRequestNumber(), observation.Sequence)
		case *notificationspb.GitHubMergeabilityEvent_SnapshotVersion:
			identity = fmt.Sprintf("github PR #%d mergeability snapshot version %d",
				provider.GithubMergeability.GetPullRequestNumber(), observation.SnapshotVersion)
		default:
			identity = "unknown event"
		}
	case *notificationspb.EventIdentity_Message:
		sender := provider.Message.GetSender()
		identity = fmt.Sprintf("message %s from %s/%s", provider.Message.GetMessageId(), sender.GetNamespace(), sender.GetName())
	default:
		identity = "unknown event"
	}
	line := invocation.Fields("entry "+strconv.FormatInt(entry.GetCursor(), 10), identity)
	if len(entry.GetSubscriptionIds()) > 0 {
		line += invocation.Fields("subscriptions", strings.Join(entry.GetSubscriptionIds(), ","))
	}
	return line
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

func (a *commands) notificationSessionID(flag string) (string, error) {
	if flag != "" {
		return flag, nil
	}
	sessionID := a.Getenv("AGENTPLANE_SESSION_ID")
	if sessionID == "" {
		return "", invocation.UsageFailure("no Session: set AGENTPLANE_SESSION_ID or pass --session ID")
	}
	return sessionID, nil
}

// parseEvents reads NAME[:ACTION[,ACTION]] filters into their canonical order.
func parseEvents(specs []string) ([]*notificationspb.GitHubEventFilter, error) {
	var filters []*notificationspb.GitHubEventFilter
	for _, spec := range specs {
		name, actions, hasActions := strings.Cut(spec, ":")
		event := notificationspb.GitHubEventName(notificationspb.GitHubEventName_value[githubEventPrefix+strings.ToUpper(name)])
		if name == "" || event == notificationspb.GitHubEventName_GITHUB_EVENT_NAME_UNSPECIFIED {
			return nil, invocation.UsageFailure("--event wants NAME[:ACTION[,ACTION]] with a GitHub event name such as issue_comment, got %q", spec)
		}
		if event == notificationspb.GitHubEventName_GITHUB_EVENT_NAME_MERGEABILITY_CHANGED && hasActions {
			return nil, invocation.UsageFailure("--event mergeability_changed does not accept actions")
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

func (a *commands) subscribePullRequestCommand() *cobra.Command {
	var events []string
	var key, sessionFlag string
	var lifetime optionalInt
	command := &cobra.Command{
		Use:   "pr OWNER/REPO#NUMBER [--event NAME[:ACTION[,ACTION]]]...",
		Short: "Follow a pull request",
		Long: `Notifications.CreateSubscription for a GitHub pull request, delivering to the inbox named by
AGENTPLANE_SESSION_ID or --session ID, which wins.
Subscribe first, then read the pull request's current state: this follows from creation, not from history.

By default, pull-request subscriptions include mergeability_changed, which reports the initial
conflicted state and later MERGEABLE/CONFLICTING transitions. --event replaces the default events (for example
--event issue_comment:created or --event mergeability_changed) rather than adding to them.
mergeability_changed takes no action suffix. The idempotency key is derived from the source, so
repeating the command returns the same subscription, and a transient failure (3) is retried by
running it again; --key overrides the key.
Exit 4 when a changed --lifetime-days (1-30, default 7) meets the same key (renew changes the
lifetime), and when the key belongs to a cancelled subscription (pass a new --key).`,
		Args: cobra.ExactArgs(1),
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			match := pullRequestPattern.FindStringSubmatch(args[0])
			if match == nil {
				return invocation.UsageFailure("want OWNER/REPO#NUMBER, got %q", args[0])
			}
			number, err := strconv.ParseInt(match[3], 10, 32)
			if err != nil {
				return invocation.UsageFailure("pull request number: %v", err)
			}
			filters, err := parseEvents(events)
			if err != nil {
				return err
			}
			sessionID, err := a.notificationSessionID(sessionFlag)
			if err != nil {
				return err
			}
			body := &notificationspb.CreateSubscriptionRequest{SessionId: sessionID}
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
			response, err := svc.CreateSubscription(ctx, body)
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response.GetSubscription().GetCancelled() {
				return invocation.ClientFailure(invocation.ExitConflict, "ALREADY_EXISTS",
					"idempotency key %s belongs to a cancelled subscription: pass a new --key", body.IdempotencyKey)
			}
			return a.showSubscription(response.GetSubscription())
		}),
	}
	command.Flags().StringArrayVar(&events, "event", nil, "NAME[:ACTION[,ACTION]]; replaces defaults; repeatable; mergeability_changed has no actions")
	command.Flags().Var(&lifetime, "lifetime-days", "subscription lifetime in days (service default 7)")
	command.Flags().StringVar(&key, "key", "", "idempotency key; derived from the source if absent")
	command.Flags().StringVar(&sessionFlag, "session", "", "Session ID, instead of AGENTPLANE_SESSION_ID")
	return command
}

func (a *commands) showSubscription(subscription *notificationspb.Subscription) error {
	if subscription == nil && a.JSON() {
		return a.ShowOne([]byte("null"), nil)
	}
	return invocation.ShowProto(a.Invocation, []*notificationspb.Subscription{subscription}, subscriptionLine, notificationJSONOptions)
}

func (a *commands) subscriptionsCommand() *cobra.Command {
	var limit int
	var after string
	command := &cobra.Command{
		Use:   "subscriptions",
		Short: "List this account's subscriptions",
		Long: `Notifications.ListSubscriptions, following server pages (128) until --limit items (default 50; 0 is unlimited).
To continue, pass the last item's id as --after.`,
		Args: cobra.NoArgs,
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, _ []string) error {
			items, err := invocation.Collect(limit, after, func(after string, _ int) ([]*notificationspb.Subscription, error) {
				request := &notificationspb.ListSubscriptionsRequest{}
				if after != "" {
					request.AfterId = proto.String(after)
				}
				response, err := svc.ListSubscriptions(ctx, request)
				if err != nil {
					return nil, invocation.GRPCFailure(ctx, err)
				}
				return response.GetSubscriptions(), nil
			}, subscriptionID)
			if err != nil {
				return err
			}
			return invocation.ShowProtoPage(a.Invocation, items, limit, subscriptionID, subscriptionLine, notificationJSONOptions)
		}),
	}
	command.Flags().IntVar(&limit, "limit", invocation.DefaultLimit, "stop after this many items; 0 is unlimited")
	command.Flags().StringVar(&after, "after", "", "continue after this subscription id")
	return command
}

func (a *commands) subscriptionCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "subscription ID",
		Short: "Show one subscription",
		Long:  "Notifications.GetSubscription: its source, version, expiry and any source error. Exit 5 if it is not this account's.",
		Args:  cobra.ExactArgs(1),
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			response, err := svc.GetSubscription(ctx, &notificationspb.GetSubscriptionRequest{SubscriptionId: args[0]})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			return a.showSubscription(response.GetSubscription())
		}),
	}
}

func (a *commands) renewCommand() *cobra.Command {
	var lifetime optionalInt
	command := &cobra.Command{
		Use:   "renew ID [--lifetime-days N]",
		Short: "Extend a subscription",
		Long: `Notifications.GetSubscription, then RenewSubscription with the version just read and the lifetime (1-30 days,
default 7). A version that changed in between exits 4: read the subscription and decide again.`,
		Args: cobra.ExactArgs(1),
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			current, err := svc.GetSubscription(ctx, &notificationspb.GetSubscriptionRequest{SubscriptionId: args[0]})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			update := &notificationspb.RenewSubscriptionRequest{SubscriptionId: args[0], Version: current.GetSubscription().GetVersion(), LifetimeDays: lifetime.value}
			response, err := svc.RenewSubscription(ctx, update)
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			return a.showSubscription(response.GetSubscription())
		}),
	}
	command.Flags().Var(&lifetime, "lifetime-days", "new lifetime in days (service default 7)")
	return command
}

func (a *commands) cancelSubscriptionCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "cancel ID",
		Short: "Cancel a subscription",
		Long: `Notifications.CancelSubscription. Idempotent. It does not cancel an Action, and accepted notices stay in
the inbox.`,
		Args: cobra.ExactArgs(1),
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			response, err := svc.CancelSubscription(ctx, &notificationspb.CancelSubscriptionRequest{SubscriptionId: args[0]})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			return a.showSubscription(response.GetSubscription())
		}),
	}
}

func (a *commands) sendCommand() *cobra.Command {
	var body, bodyFile, fromSession, key string
	command := &cobra.Command{
		Use:   "send SESSION_ID {--body TEXT | --body-file FILE} [--from-session ID] [--key KEY]",
		Short: "Message another Session",
		Long: `Notifications.SendMessage: puts the text in the inbox of that Session, which notices it like any
entry. A session_inbox_access rule bound to your ServiceAccount must allow send_message to the Sandbox,
else exit 1 (PERMISSION_DENIED). The service resolves the Session's current Sandbox and exact UID.
The text is sent as given, from --body or
--body-file (- reads stdin). --from-session is the label of the Session you write from, so the recipient
can answer you; it defaults to AGENTPLANE_SESSION_ID, and nothing verifies it.

Without --key a random idempotency key is generated and written to stderr before the POST. Retry a
failed or uncertain send with --key KEY and the same message: the service answers with the message it
already accepted. The key with another message exits 4. Exit 5 when there is no such Sandbox or
incarnation, or no record of the Session in it. Exit 3 is UNAVAILABLE (retry with the key) or
RESOURCE_EXHAUSTED (too many of your messages there await acknowledgement: wait, do not send more).`,
		Args: cobra.ExactArgs(1),
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			message := &notificationspb.SendMessageRequest{
				SessionId: args[0], IdempotencyKey: key, Body: body,
			}
			if bodyFile != "" {
				text, err := a.readBody(bodyFile)
				if err != nil {
					return err
				}
				message.Body = text
			}
			if fromSession == "" && a.Getenv("AGENTPLANE_SESSION_ID") != "" {
				own, err := a.notificationSessionID("")
				if err != nil {
					return err
				}
				fromSession = own
			}
			if fromSession != "" {
				message.FromSession = &fromSession
			}
			if message.IdempotencyKey == "" {
				message.IdempotencyKey = uuid.NewString()
				a.AnnounceKey(message.IdempotencyKey)
			}
			response, err := svc.SendMessage(ctx, message)
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			return a.showMessage(response.GetMessage())
		}),
	}
	command.Flags().StringVar(&body, "body", "", "the message text")
	command.Flags().StringVar(&bodyFile, "body-file", "", "read the message text from FILE; - reads stdin")
	command.MarkFlagsOneRequired("body", "body-file")
	command.MarkFlagsMutuallyExclusive("body", "body-file")
	command.Flags().StringVar(&fromSession, "from-session", "", "your Session's label; defaults to AGENTPLANE_SESSION_ID")
	command.Flags().StringVar(&key, "key", "", "idempotency key; generated and printed on stderr if absent")
	return command
}

// readBody reads --body-file: a file, or stdin for -.
func (a *commands) readBody(path string) (string, error) {
	var data []byte
	var err error
	if path == "-" {
		data, err = io.ReadAll(a.Stdin())
	} else {
		data, err = os.ReadFile(path)
	}
	if err != nil {
		return "", invocation.UsageFailure("--body-file: %v", err)
	}
	return string(data), nil
}

func (a *commands) messageCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "message ID",
		Short: "Follow a message you sent",
		Long: `Notifications.GetMessage: whether a notice named it to the recipient (covered), whether the recipient
acknowledged its inbox through it (acknowledged) and whether that inbox was retired. The facts are
independent, and none says the recipient did what you asked. Exit 5 for an unknown ID, a message another
account sent or a purged inbox; it never means the message was not delivered.`,
		Args: cobra.ExactArgs(1),
		RunE: a.notificationCall(func(ctx context.Context, svc notificationspb.NotificationsClient, args []string) error {
			response, err := svc.GetMessage(ctx, &notificationspb.GetMessageRequest{MessageId: args[0]})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			return a.showMessage(response.GetMessage())
		}),
	}
}

func (a *commands) showMessage(message *notificationspb.Message) error {
	if message == nil && a.JSON() {
		return a.ShowOne([]byte("null"), nil)
	}
	return invocation.ShowProto(a.Invocation, []*notificationspb.Message{message}, messageLine, notificationJSONOptions)
}

func messageLine(message *notificationspb.Message) (string, error) {
	if message == nil {
		return "", invocation.InvalidResponse("no message in the response")
	}
	acceptedAt := message.GetAcceptedAt()
	if acceptedAt != nil {
		if err := acceptedAt.CheckValid(); err != nil {
			return "", invocation.InvalidResponse("accepted_at: %v", err)
		}
	}
	line := invocation.Fields(message.GetId(), fmt.Sprintf("to session %s", message.GetSessionId()),
		"accepted "+acceptedAt.AsTime().UTC().Format(time.RFC3339))
	if message.GetCovered() {
		line += "  covered"
	}
	if message.GetAcknowledged() {
		line += "  acknowledged"
	}
	if message.GetInboxRetired() {
		line += "  inbox retired"
	}
	return line, nil
}

func subscriptionLine(subscription *notificationspb.Subscription) (string, error) {
	if subscription == nil {
		return "", invocation.InvalidResponse("no subscription in the response")
	}
	line := invocation.Fields(subscription.Id, sourceLabel(subscription.Source), "v"+strconv.FormatInt(subscription.GetVersion(), 10))
	if subscription.ExpiresAt != nil {
		if err := subscription.ExpiresAt.CheckValid(); err != nil {
			return "", invocation.InvalidResponse("expires_at: %v", err)
		}
		line = invocation.Fields(line, "expires "+subscription.ExpiresAt.AsTime().UTC().Format(time.RFC3339))
	}
	if subscription.Cancelled {
		line += "  cancelled"
	}
	if subscription.ErrorKind != nil {
		line += "  error " + invocation.EnumSuffix(subscription.ErrorKind.String(), failureKindPrefix)
	}
	if github := subscription.GetGithub(); github != nil {
		status, err := invocation.ProtoJSON(github)
		if err != nil {
			return "", err
		}
		line = invocation.Fields(line, "github status "+string(status))
	}

	return line, nil
}

func subscriptionID(subscription *notificationspb.Subscription) (string, error) {
	return subscription.GetId(), nil
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

type commands struct{ *invocation.Invocation }

func Command(in *invocation.Invocation) *cobra.Command {
	return (&commands{Invocation: in}).notificationsCommand()
}
