package main

import (
	"cmp"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
)

const notificationsHelp = `Calls the Notification Service (AGENTPLANE_NOTIFICATIONS_URL, AGENTPLANE_NOTIFICATIONS_CREDENTIAL).

Subscriptions deliver notices to a Session inbox, so a long wait costs no turn; send puts a message in
another Session's inbox. Inbox reads are non-destructive; acknowledgement advances the handled prefix.
No command here has an outcome exit code; cancel removes a subscription and never cancels an Action.`

const (
	githubEventPrefix  = "GITHUB_EVENT_NAME_"
	failureKindPrefix  = "SOURCE_FAILURE_KIND_"
	subscriptionsRoute = "/v1/subscriptions"
	inboxesRoute       = "/v1/inboxes"
	messagesRoute      = "/v1/messages"
)

var pullRequestPattern = regexp.MustCompile(`^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)$`)

func (a *app) notificationsCommand() *cobra.Command {
	return group("notifications", "Follow events, subscriptions and inboxes", notificationsHelp,
		group("subscribe", "Subscribe to a source", "Creates a subscription for this Session's inbox.",
			group("github", "Subscribe to GitHub activity", "Only pull requests (`pr`) are supported.", a.subscribePullRequestCommand())),
		a.inboxCommand(), a.subscriptionsCommand(), a.subscriptionCommand(), a.renewCommand(), a.cancelSubscriptionCommand(),
		a.sendCommand(), a.messageCommand())
}

func (a *app) inboxCommand() *cobra.Command {
	return group("inbox", "Read and acknowledge inbox entries", "Reading is non-destructive; acknowledgement advances the handled prefix.",
		a.readInboxCommand(), a.ackInboxCommand())
}

func (a *app) readInboxCommand() *cobra.Command {
	var after int64
	var limit int
	command := &cobra.Command{
		Use:   "read ID [--after CURSOR] [--limit N]",
		Short: "Read one page of inbox entries",
		Long: `GET /v1/inboxes/{id}/entries. Reads are non-destructive and return one contiguous page.
Continue after the last returned entry's cursor. Text output summarizes the entries; --json prints the
full response, including provider payloads and the latest notice state. The service returns at most 128
entries per page, and expired payloads are reported in inbox.expiredThrough.`,
		Args: cobra.ExactArgs(1),
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, args []string) error {
			if after < 0 {
				return usageFailure("--after must not be negative")
			}
			if limit < 1 || limit > maxPage {
				return usageFailure("--limit must be between 1 and %d", maxPage)
			}
			query := url.Values{}
			query.Set("afterCursor", strconv.FormatInt(after, 10))
			query.Set("limit", strconv.Itoa(limit))
			response, err := svc.fetch(ctx, inboxesRoute+"/"+url.PathEscape(args[0])+"/entries", query)
			if err != nil {
				return err
			}
			return a.showOne(response, inboxPageLine)
		}),
	}
	command.Flags().Int64Var(&after, "after", 0, "read entries with cursors greater than CURSOR")
	command.Flags().IntVar(&limit, "limit", maxPage, "maximum entries in this page (1-128)")
	return command
}

func (a *app) ackInboxCommand() *cobra.Command {
	var through int64
	command := &cobra.Command{
		Use:   "ack ID --through CURSOR",
		Short: "Acknowledge a handled inbox prefix",
		Long: `PUT /v1/inboxes/{id}/acknowledgement. Acknowledgement covers every entry through the cursor,
across all subscriptions in this inbox. Read and handle the whole contiguous prefix first. The service
never moves the cursor backwards and refuses a cursor beyond the inbox's last entry.`,
		Args: cobra.ExactArgs(1),
		PreRunE: func(cmd *cobra.Command, _ []string) error {
			if !cmd.Flags().Changed("through") {
				return usageFailure("--through is required")
			}
			if through < 0 {
				return usageFailure("--through must not be negative")
			}
			return nil
		},
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, args []string) error {
			body := struct {
				ThroughCursor string `json:"throughCursor"`
			}{ThroughCursor: strconv.FormatInt(through, 10)}
			response, err := svc.do(ctx, request{
				method: http.MethodPut,
				path:   inboxesRoute + "/" + url.PathEscape(args[0]) + "/acknowledgement",
				body:   body,
			})
			if err != nil {
				return err
			}
			inbox, err := responseField(response, "inbox")
			if err != nil {
				return err
			}
			return a.showOne(inbox, inboxLine)
		}),
	}
	command.Flags().Int64Var(&through, "through", 0, "last cursor you have handled; required")
	return command
}

func inboxPageLine(raw json.RawMessage) (string, error) {
	var page notificationspb.ReadInboxResponse
	if err := decodeMessage(raw, &page); err != nil {
		return "", err
	}
	if page.GetInbox().GetId() == "" {
		return "", invalidResponse("no inbox in the response")
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

func inboxLine(raw json.RawMessage) (string, error) {
	var inbox notificationspb.Inbox
	if err := decodeMessage(raw, &inbox); err != nil {
		return "", err
	}
	if inbox.GetId() == "" {
		return "", invalidResponse("no inbox ID in the response")
	}
	return inboxSummary(&inbox), nil
}

func inboxSummary(inbox *notificationspb.Inbox) string {
	line := fields(
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
		line += fields("delivery error", strconv.Quote(inbox.GetDeliveryError()))
	}
	return line
}

func noticeLine(notice *notificationspb.Notice) string {
	line := fields(
		"notice through", strconv.FormatInt(notice.GetThroughCursor(), 10),
		"attempted", strconv.FormatBool(notice.GetAttempted()),
		"admitted", strconv.FormatBool(notice.GetAdmitted()),
		"confirmed", strconv.FormatBool(notice.GetConfirmed()),
	)
	if notice.GetError() != "" {
		line += fields("error", strconv.Quote(notice.GetError()))
	}
	return line
}

func inboxEntryLine(entry *notificationspb.InboxEntry) string {
	var identity string
	switch provider := entry.GetEvent().GetProvider().(type) {
	case *notificationspb.EventIdentity_Actions:
		identity = fmt.Sprintf("actions %s sequence %d", provider.Actions.GetRequestId(), provider.Actions.GetSequence())
	case *notificationspb.EventIdentity_Github:
		identity = "github " + enumSuffix(provider.Github.GetEvent().String(), githubEventPrefix)
		if action := provider.Github.GetAction(); action != "" {
			identity += " " + strconv.Quote(action)
		}
	case *notificationspb.EventIdentity_GithubObservation:
		identity = fmt.Sprintf("github %s observation sequence %d",
			enumSuffix(provider.GithubObservation.GetEvent().String(), githubEventPrefix),
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
	line := fields("entry "+strconv.FormatInt(entry.GetCursor(), 10), identity)
	if len(entry.GetSubscriptionIds()) > 0 {
		line += fields("subscriptions", strings.Join(entry.GetSubscriptionIds(), ","))
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
		name, actions, hasActions := strings.Cut(spec, ":")
		event := notificationspb.GitHubEventName(notificationspb.GitHubEventName_value[githubEventPrefix+strings.ToUpper(name)])
		if name == "" || event == notificationspb.GitHubEventName_GITHUB_EVENT_NAME_UNSPECIFIED {
			return nil, usageFailure("--event wants NAME[:ACTION[,ACTION]] with a GitHub event name such as issue_comment, got %q", spec)
		}
		if event == notificationspb.GitHubEventName_GITHUB_EVENT_NAME_MERGEABILITY_CHANGED && hasActions {
			return nil, usageFailure("--event mergeability_changed does not accept actions")
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

By default, pull-request subscriptions include mergeability_changed, which reports the initial
conflicted state and later MERGEABLE/CONFLICTING transitions. --event replaces the default events (for example
--event issue_comment:created or --event mergeability_changed) rather than adding to them.
mergeability_changed takes no action suffix. The idempotency key is derived from the source, so
repeating the command returns the same subscription, and a transient failure (3) is retried by
running it again; --key overrides the key.
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
	command.Flags().StringArrayVar(&events, "event", nil, "NAME[:ACTION[,ACTION]]; replaces defaults; repeatable; mergeability_changed has no actions")
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

func (a *app) sendCommand() *cobra.Command {
	var body, bodyFile, uid, fromSession, key string
	command := &cobra.Command{
		Use:   "send NAMESPACE/SANDBOX SESSION_ID {--body TEXT | --body-file FILE} [--uid UID] [--from-session ID] [--key KEY]",
		Short: "Message another Session",
		Long: `POST /v1/messages: puts the text in the inbox of that Session of that Sandbox, which notices it like any
entry. A session_inbox_access rule bound to your ServiceAccount must allow send_message to the Sandbox,
else exit 1 (PERMISSION_DENIED). --uid sends only to that incarnation; without it the message goes to
the Sandbox of that name now, and the answer carries its UID. The text is sent as given, from --body or
--body-file (- reads stdin). --from-session is the label of the Session you write from, so the recipient
can answer you; it defaults to the session_id of AGENTPLANE_DESTINATION, and nothing verifies it.

Without --key a random idempotency key is generated and written to stderr before the POST. Retry a
failed or uncertain send with --key KEY and the same message: the service answers with the message it
already accepted. The key with another message exits 4. Exit 5 when there is no such Sandbox or
incarnation, or no record of the Session in it. Exit 3 is UNAVAILABLE (retry with the key) or
RESOURCE_EXHAUSTED (too many of your messages there await acknowledgement: wait, do not send more).`,
		Args: cobra.ExactArgs(2),
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, args []string) error {
			namespace, name, ok := strings.Cut(args[0], "/")
			if !ok || namespace == "" || name == "" || strings.Contains(name, "/") {
				return usageFailure("want NAMESPACE/SANDBOX, got %q", args[0])
			}
			message := &notificationspb.SendMessageRequest{
				DestinationRef: &notificationspb.DestinationRef{Namespace: namespace, Name: name, Uid: uid},
				SessionId:      args[1], IdempotencyKey: key, Body: body,
			}
			if bodyFile != "" {
				text, err := a.readBody(bodyFile)
				if err != nil {
					return err
				}
				message.Body = text
			}
			if fromSession == "" && a.getenv("AGENTPLANE_DESTINATION") != "" {
				own, err := a.destination("")
				if err != nil {
					return err
				}
				fromSession = own.GetSessionId()
			}
			if fromSession != "" {
				message.FromSession = &fromSession
			}
			if message.IdempotencyKey == "" {
				message.IdempotencyKey = uuid.NewString()
				a.announceKey(message.IdempotencyKey)
			}
			response, err := svc.do(ctx, request{method: http.MethodPost, path: messagesRoute, body: message, conflict: "ALREADY_EXISTS"})
			if err != nil {
				return err
			}
			return a.showMessage(response)
		}),
	}
	command.Flags().StringVar(&body, "body", "", "the message text")
	command.Flags().StringVar(&bodyFile, "body-file", "", "read the message text from FILE; - reads stdin")
	command.MarkFlagsOneRequired("body", "body-file")
	command.MarkFlagsMutuallyExclusive("body", "body-file")
	command.Flags().StringVar(&uid, "uid", "", "send only to this incarnation of the Sandbox")
	command.Flags().StringVar(&fromSession, "from-session", "", "your Session's label; defaults to AGENTPLANE_DESTINATION's session_id")
	command.Flags().StringVar(&key, "key", "", "idempotency key; generated and printed on stderr if absent")
	return command
}

// readBody reads --body-file: a file, or stdin for -.
func (a *app) readBody(path string) (string, error) {
	var data []byte
	var err error
	if path == "-" {
		data, err = io.ReadAll(a.stdin)
	} else {
		data, err = os.ReadFile(path)
	}
	if err != nil {
		return "", usageFailure("--body-file: %v", err)
	}
	return string(data), nil
}

func (a *app) messageCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "message ID",
		Short: "Follow a message you sent",
		Long: `GET /v1/messages/{id}: whether a notice named it to the recipient (covered), whether the recipient
acknowledged its inbox through it (acknowledged) and whether that inbox was retired. The facts are
independent, and none says the recipient did what you asked. Exit 5 for an unknown ID, a message another
account sent or a purged inbox; it never means the message was not delivered.`,
		Args: cobra.ExactArgs(1),
		RunE: a.command("NOTIFICATIONS", func(ctx context.Context, svc *service, args []string) error {
			body, err := svc.fetch(ctx, messagesRoute+"/"+url.PathEscape(args[0]), nil)
			if err != nil {
				return err
			}
			return a.showMessage(body)
		}),
	}
}

// showMessage prints the message of a single-message response.
func (a *app) showMessage(response []byte) error {
	message, err := responseField(response, "message")
	if err != nil {
		return err
	}
	return a.showOne(message, messageLine)
}

func messageLine(raw json.RawMessage) (string, error) {
	var message notificationspb.Message
	if err := decodeMessage(raw, &message); err != nil {
		return "", err
	}
	ref := message.GetDestinationRef()
	line := fields(message.GetId(), fmt.Sprintf("to %s/%s session %s", ref.GetNamespace(), ref.GetName(), message.GetSessionId()),
		"accepted "+message.GetAcceptedAt().AsTime().UTC().Format(time.RFC3339))
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
	if github := subscription.GetGithub(); github != nil {
		if ci := github.GetCi(); ci != nil {
			if ci.GetLastSuccessAt() != nil {
				line += fields("CI last refresh", ci.GetLastSuccessAt().AsTime().UTC().Format(time.RFC3339))
			}
			if ci.ErrorKind != nil {
				line += fields("CI error", enumSuffix(ci.GetErrorKind().String(), failureKindPrefix))
			}
			if ci.GetRetryAt() != nil {
				line += fields("CI retry", ci.GetRetryAt().AsTime().UTC().Format(time.RFC3339))
			}
			if ci.GetRefreshingUntil() != nil {
				line += fields("CI refreshing until", ci.GetRefreshingUntil().AsTime().UTC().Format(time.RFC3339))
			}
		}
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
