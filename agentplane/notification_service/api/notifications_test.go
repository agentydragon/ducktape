package api

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/notification_service/github"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/google/uuid"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestEveryRPCRefusesACallerWhoseBearerIsNotAWorkloadOfTheNamespace(t *testing.T) {
	server := newServer(t)
	rpcs := map[string]func(context.Context) error{
		"CreateSubscription": unary(server.client.CreateSubscription),
		"ListSubscriptions":  unary(server.client.ListSubscriptions),
		"GetSubscription":    unary(server.client.GetSubscription),
		"RenewSubscription":  unary(server.client.RenewSubscription),
		"CancelSubscription": unary(server.client.CancelSubscription),
		"ListInboxes":        unary(server.client.ListInboxes),
		"ReadInbox":          unary(server.client.ReadInbox),
		"AcknowledgeInbox":   unary(server.client.AcknowledgeInbox),
		"RetireInbox":        unary(server.client.RetireInbox),
		"SendMessage":        unary(server.client.SendMessage),
		"GetMessage":         unary(server.client.GetMessage),
	}
	for name, call := range rpcs {
		for bearer, token := range map[string]string{"no bearer": "", "unknown bearer": "test-token-unknown", "other namespace": outsiderToken} {
			t.Run(name+" with "+bearer, func(t *testing.T) {
				requireCode(t, call(bearing(t, token)), codes.Unauthenticated)
			})
		}
	}
}

func TestCreateSubscriptionFollowsAnActionRequestAndAnAgentFindsItsInbox(t *testing.T) {
	server := newServer(t)
	request := createRequest("create")
	response, err := server.client.CreateSubscription(bearing(t, sandboxToken), request)
	if err != nil {
		t.Fatal(err)
	}
	subscription := response.GetSubscription()
	if _, err := uuid.Parse(subscription.GetId()); err != nil {
		t.Errorf("id %q: %v", subscription.GetId(), err)
	}
	if subscription.GetVersion() != 1 || subscription.GetCancelled() || subscription.GetIdempotencyKey() != "create" ||
		!proto.Equal(subscription.GetSource(), request.GetSource()) || subscription.GetGithub() != nil {
		t.Errorf("subscription = %v", subscription)
	}
	if lifetime := time.Until(subscription.GetExpiresAt().AsTime()); lifetime < 6*24*time.Hour || lifetime > 7*24*time.Hour {
		t.Errorf("a request naming no lifetime expires in %v, want 7 days", lifetime)
	}
	// The Action Service was asked, as the caller, whether it may follow the request.
	source := request.GetSource().GetActions()
	want := actionsCall{owner: action.ServiceAccount{Namespace: testNamespace, Name: sandboxName}, requestID: uuid.MustParse(source.GetRequestId())}
	if got := server.actions.asked(); len(got) != 1 || got[0] != want {
		t.Errorf("Action Service asked %v, want %v", got, want)
	}

	inboxes, err := server.client.ListInboxes(bearing(t, sandboxToken), &notificationspb.ListInboxesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(inboxes.GetInboxes()) != 1 {
		t.Fatalf("inboxes = %v", inboxes)
	}
	inbox := inboxes.GetInboxes()[0]
	if inbox.GetId() != subscription.GetInboxId() || inbox.GetSessionId() != testSession ||
		!proto.Equal(inbox.GetDestinationRef(), sandboxRef()) || inbox.GetLastCursor() != 0 || inbox.GetRetired() {
		t.Errorf("inbox = %v", inbox)
	}
}

func TestCreateSubscriptionReplaysItsIdempotencyKey(t *testing.T) {
	server := newServer(t)
	request := createRequest("replay")
	first, err := server.client.CreateSubscription(bearing(t, sandboxToken), request)
	if err != nil {
		t.Fatal(err)
	}
	replayed, err := server.client.CreateSubscription(bearing(t, sandboxToken), request)
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(replayed.GetSubscription(), first.GetSubscription()) {
		t.Errorf("replay = %v, want %v", replayed, first)
	}
	other := createRequest("replay")
	_, err = server.client.CreateSubscription(bearing(t, sandboxToken), other)
	requireCode(t, err, codes.AlreadyExists)
}

func TestCreateSubscriptionRefusesWhatTheCallerCannotFollowAndStoresNothing(t *testing.T) {
	cases := []struct {
		name string
		// token defaults to the test Sandbox's.
		token      string
		change     func(*notificationspb.CreateSubscriptionRequest)
		want       codes.Code
		asksRunner bool
	}{
		{"destination in another namespace", "", func(r *notificationspb.CreateSubscriptionRequest) { r.DestinationRef.Namespace = "test-elsewhere" }, codes.NotFound, false},
		{"destination Sandbox that does not exist", "", func(r *notificationspb.CreateSubscriptionRequest) { r.DestinationRef.Name = "no-such-sandbox" }, codes.NotFound, false},
		{"destination Sandbox that was replaced", "", func(r *notificationspb.CreateSubscriptionRequest) { r.DestinationRef.Uid = "replaced-uid" }, codes.NotFound, false},
		{"another account's Sandbox", otherSandboxToken, func(*notificationspb.CreateSubscriptionRequest) {}, codes.NotFound, false},
		{"Session the runner does not hold", "", func(r *notificationspb.CreateSubscriptionRequest) { r.SessionId = "missing-session" }, codes.NotFound, true},
		{"no destination", "", func(r *notificationspb.CreateSubscriptionRequest) { r.DestinationRef = nil }, codes.InvalidArgument, false},
		{"destination without a UID", "", func(r *notificationspb.CreateSubscriptionRequest) { r.DestinationRef.Uid = "" }, codes.InvalidArgument, false},
		{"no source", "", func(r *notificationspb.CreateSubscriptionRequest) { r.Source = nil }, codes.InvalidArgument, false},
		{"Action request that is no UUID", "", func(r *notificationspb.CreateSubscriptionRequest) { r.Source.GetActions().RequestId = "request" }, codes.InvalidArgument, false},
		{"negative sequence", "", func(r *notificationspb.CreateSubscriptionRequest) { r.Source.GetActions().AfterSequence = -1 }, codes.InvalidArgument, false},
		{"no idempotency key", "", func(r *notificationspb.CreateSubscriptionRequest) { r.IdempotencyKey = "" }, codes.InvalidArgument, false},
		{"no Session", "", func(r *notificationspb.CreateSubscriptionRequest) { r.SessionId = "" }, codes.InvalidArgument, false},
		{"lifetime below a day", "", func(r *notificationspb.CreateSubscriptionRequest) { r.LifetimeDays = proto.Int32(0) }, codes.InvalidArgument, false},
		{"lifetime beyond 30 days", "", func(r *notificationspb.CreateSubscriptionRequest) { r.LifetimeDays = proto.Int32(31) }, codes.InvalidArgument, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			server := newServer(t)
			request := createRequest("refused")
			c.change(request)
			token := c.token
			if token == "" {
				token = sandboxToken
			}
			_, err := server.client.CreateSubscription(bearing(t, token), request)
			requireCode(t, err, c.want)
			if asked := len(server.runner.questions()) > 0; asked != c.asksRunner {
				t.Errorf("runner asked = %v, want %v", asked, c.asksRunner)
			}
			if len(server.actions.asked()) != 0 {
				t.Errorf("Action Service asked about a subscription that was refused before: %v", server.actions.asked())
			}
			listed, err := server.client.ListSubscriptions(bearing(t, token), &notificationspb.ListSubscriptionsRequest{})
			if err != nil {
				t.Fatal(err)
			}
			if len(listed.GetSubscriptions()) != 0 {
				t.Errorf("a refused creation left %v", listed.GetSubscriptions())
			}
		})
	}
}

func TestCreateSubscriptionAnswersAFaultOfACollaboratorWithoutItsText(t *testing.T) {
	cases := []struct {
		name  string
		fail  func(testServer, error)
		cause error
		want  codes.Code
	}{
		{"runner unreachable", func(s testServer, err error) { s.runner.failWith(err) }, errors.New("dial runner-secret-host"), codes.Unavailable},
		{"Action request of another account", func(s testServer, err error) { s.actions.failWith(err) }, store.ErrNotFound, codes.NotFound},
		{"Action Service unreachable", func(s testServer, err error) { s.actions.failWith(err) }, errors.New("dial actions-secret-host"), codes.Unavailable},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			server := newServer(t)
			c.fail(server, c.cause)
			_, err := server.client.CreateSubscription(bearing(t, sandboxToken), createRequest("faulted"))
			requireCode(t, err, c.want)
			if strings.Contains(status.Convert(err).Message(), "secret") {
				t.Errorf("the client was told %q", status.Convert(err).Message())
			}
			listed, err := server.client.ListSubscriptions(bearing(t, sandboxToken), &notificationspb.ListSubscriptionsRequest{})
			if err != nil || len(listed.GetSubscriptions()) != 0 {
				t.Errorf("listed %v, %v", listed, err)
			}
		})
	}
}

func TestACallThatOutlastsTheRequestTimeoutFails(t *testing.T) {
	server := newServer(t, func(c *serverConfig) { c.requestTimeout = 100 * time.Millisecond })
	server.runner.hang()
	_, err := server.client.CreateSubscription(bearing(t, sandboxToken), createRequest("hangs"))
	requireCode(t, err, codes.DeadlineExceeded)
}

func githubRequest(key string) *notificationspb.CreateSubscriptionRequest {
	request := createRequest(key)
	request.Source = &notificationspb.Source{Provider: &notificationspb.Source_Github{Github: &notificationspb.GitHubSource{
		Repository: "test-owner/test-repo",
		Subject: &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_PullRequest{
			PullRequest: &notificationspb.PullRequestSubject{Number: 7},
		}},
		Events: []*notificationspb.GitHubEventFilter{
			{Event: notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST, Actions: []string{"opened", "closed"}},
			{Event: notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CHECK_RUN},
		},
	}}}
	return request
}

func TestCreateSubscriptionFollowsAGitHubSubjectUnderTheAccessItWasAuthorizedWith(t *testing.T) {
	server := newServer(t)
	request := githubRequest("github")
	response, err := server.client.CreateSubscription(bearing(t, sandboxToken), request)
	if err != nil {
		t.Fatal(err)
	}
	subscription := response.GetSubscription()
	// The store keeps event filters in a canonical order, filters by event name and actions alphabetically.
	canonical := proto.Clone(request.GetSource()).(*notificationspb.Source)
	events := canonical.GetGithub().GetEvents()
	events[0], events[1] = events[1], events[0]
	slices.Sort(events[1].Actions)
	if !proto.Equal(subscription.GetSource(), canonical) {
		t.Errorf("source = %v, want %v", subscription.GetSource(), canonical)
	}
	access := subscription.GetGithub().GetAccess()
	if len(access) != 1 || access[0].GetAppId() != 11 || access[0].GetInstallationId() != 22 || access[0].GetRepositoryId() != 33 {
		t.Errorf("access = %v", access)
	}
	sources := server.github.authorized()
	if len(sources) != 1 || sources[0].Repository != "test-owner/test-repo" || sources[0].Subject != (store.PullRequestSubject{Number: 7}) {
		t.Errorf("GitHub was asked to authorize %v", sources)
	}
}

func TestCreateSubscriptionRefusesAGitHubSourceWhileTheProviderIsOff(t *testing.T) {
	server := newServer(t, func(c *serverConfig) { c.githubDisabled = true })
	_, err := server.client.CreateSubscription(bearing(t, sandboxToken), githubRequest("disabled"))
	requireCode(t, err, codes.PermissionDenied)
	// An Action request is not a GitHub source.
	if _, err := server.client.CreateSubscription(bearing(t, sandboxToken), createRequest("actions")); err != nil {
		t.Fatal(err)
	}
}

func TestCreateSubscriptionMapsWhatGitHubRefusesToStatusCodes(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want codes.Code
	}{
		{"access lost", &github.AccessError{StatusCode: 403}, codes.PermissionDenied},
		{"installation missing", &github.NotInstalledError{AccessError: github.AccessError{StatusCode: 404}}, codes.PermissionDenied},
		{"installation suspended", &github.SuspendedError{AccessError: github.AccessError{StatusCode: 403}}, codes.PermissionDenied},
		{"repository changed", &github.SourceChangedError{Reason: "the repository was renamed"}, codes.PermissionDenied},
		{"rate limited", &github.RateLimitedError{StatusCode: 429, RetrySeconds: 42}, codes.Unavailable},
		{"unexpected status", &github.StatusError{StatusCode: 502}, codes.Unavailable},
		{"no response", &github.TransportError{Err: errors.New("dial github-secret-host")}, codes.Unavailable},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			server := newServer(t)
			server.github.failWith(c.err)
			_, err := server.client.CreateSubscription(bearing(t, sandboxToken), githubRequest("refused"))
			requireCode(t, err, c.want)
			if strings.Contains(status.Convert(err).Message(), "secret") {
				t.Errorf("the client was told %q", status.Convert(err).Message())
			}
			var retry *errdetails.RetryInfo
			for _, detail := range status.Convert(err).Details() {
				if info, ok := detail.(*errdetails.RetryInfo); ok {
					retry = info
				}
			}
			limited := &github.RateLimitedError{}
			if errors.As(c.err, &limited) {
				if retry.GetRetryDelay().AsDuration() != 42*time.Second {
					t.Errorf("retry delay = %v, want 42s", retry.GetRetryDelay().AsDuration())
				}
			} else if retry != nil {
				t.Errorf("retry info %v on a failure that is not a rate limit", retry)
			}
			listed, err := server.client.ListSubscriptions(bearing(t, sandboxToken), &notificationspb.ListSubscriptionsRequest{})
			if err != nil || len(listed.GetSubscriptions()) != 0 {
				t.Errorf("listed %v, %v", listed, err)
			}
		})
	}
}

func TestRenewAndCancelFollowTheSubscriptionsVersion(t *testing.T) {
	server := newServer(t)
	created := server.subscribe(t, "renew")
	renewed, err := server.client.RenewSubscription(bearing(t, sandboxToken), &notificationspb.RenewSubscriptionRequest{
		SubscriptionId: created.GetId(), Version: created.GetVersion(), LifetimeDays: proto.Int32(30),
	})
	if err != nil {
		t.Fatal(err)
	}
	subscription := renewed.GetSubscription()
	if subscription.GetVersion() != 2 || !subscription.GetExpiresAt().AsTime().After(created.GetExpiresAt().AsTime()) ||
		!proto.Equal(subscription.GetSource(), created.GetSource()) {
		t.Errorf("renewed = %v, created %v", subscription, created)
	}
	got, err := server.client.GetSubscription(bearing(t, sandboxToken), &notificationspb.GetSubscriptionRequest{SubscriptionId: created.GetId()})
	if err != nil {
		t.Fatal(err)
	}
	listed, err := server.client.ListSubscriptions(bearing(t, sandboxToken), &notificationspb.ListSubscriptionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(got.GetSubscription(), subscription) || len(listed.GetSubscriptions()) != 1 || !proto.Equal(listed.GetSubscriptions()[0], subscription) {
		t.Errorf("get = %v, list = %v, want %v", got, listed, subscription)
	}

	// A renewal of a version that has moved on is refused, as is a renewal of a cancelled subscription.
	_, err = server.client.RenewSubscription(bearing(t, sandboxToken), &notificationspb.RenewSubscriptionRequest{
		SubscriptionId: created.GetId(), Version: created.GetVersion(),
	})
	requireCode(t, err, codes.Aborted)
	cancelled, err := server.client.CancelSubscription(bearing(t, sandboxToken), &notificationspb.CancelSubscriptionRequest{SubscriptionId: created.GetId()})
	if err != nil {
		t.Fatal(err)
	}
	if !cancelled.GetSubscription().GetCancelled() {
		t.Errorf("cancelled = %v", cancelled)
	}
	_, err = server.client.RenewSubscription(bearing(t, sandboxToken), &notificationspb.RenewSubscriptionRequest{
		SubscriptionId: created.GetId(), Version: cancelled.GetSubscription().GetVersion(),
	})
	requireCode(t, err, codes.Aborted)
	again, err := server.client.CancelSubscription(bearing(t, sandboxToken), &notificationspb.CancelSubscriptionRequest{SubscriptionId: created.GetId()})
	if err != nil || !proto.Equal(again.GetSubscription(), cancelled.GetSubscription()) {
		t.Errorf("cancelling again = %v, %v", again, err)
	}
}

func TestRenewRefusesWhatIsNotAVersionOrALifetime(t *testing.T) {
	server := newServer(t)
	created := server.subscribe(t, "renew-invalid")
	for name, request := range map[string]*notificationspb.RenewSubscriptionRequest{
		"no UUID":     {SubscriptionId: "subscription", Version: 1},
		"no version":  {SubscriptionId: created.GetId()},
		"no lifetime": {SubscriptionId: created.GetId(), Version: 1, LifetimeDays: proto.Int32(0)},
		"too long":    {SubscriptionId: created.GetId(), Version: 1, LifetimeDays: proto.Int32(31)},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := server.client.RenewSubscription(bearing(t, sandboxToken), request)
			requireCode(t, err, codes.InvalidArgument)
		})
	}
}

func TestListSubscriptionsContinuesAfterTheLastIDOfAPage(t *testing.T) {
	server := newServer(t)
	for _, key := range []string{"first", "second", "third"} {
		server.subscribe(t, key)
	}
	all, err := server.client.ListSubscriptions(bearing(t, sandboxToken), &notificationspb.ListSubscriptionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(all.GetSubscriptions()) != 3 {
		t.Fatalf("listed %v", all)
	}
	after := all.GetSubscriptions()[0].GetId()
	rest, err := server.client.ListSubscriptions(bearing(t, sandboxToken), &notificationspb.ListSubscriptionsRequest{AfterId: &after})
	if err != nil {
		t.Fatal(err)
	}
	if len(rest.GetSubscriptions()) != 2 || !proto.Equal(rest.GetSubscriptions()[0], all.GetSubscriptions()[1]) ||
		!proto.Equal(rest.GetSubscriptions()[1], all.GetSubscriptions()[2]) {
		t.Errorf("after %s listed %v, want the last two of %v", after, rest, all)
	}
	bad := "not-a-uuid"
	_, err = server.client.ListSubscriptions(bearing(t, sandboxToken), &notificationspb.ListSubscriptionsRequest{AfterId: &bad})
	requireCode(t, err, codes.InvalidArgument)
}

// ownedCalls are the calls about a subscription and its inbox, each of which only their owner may make.
func ownedCalls(server testServer, subscription *notificationspb.Subscription) map[string]func(context.Context) error {
	return map[string]func(context.Context) error{
		"GetSubscription": func(ctx context.Context) error {
			_, err := server.client.GetSubscription(ctx, &notificationspb.GetSubscriptionRequest{SubscriptionId: subscription.GetId()})
			return err
		},
		"RenewSubscription": func(ctx context.Context) error {
			_, err := server.client.RenewSubscription(ctx, &notificationspb.RenewSubscriptionRequest{SubscriptionId: subscription.GetId(), Version: 1})
			return err
		},
		"CancelSubscription": func(ctx context.Context) error {
			_, err := server.client.CancelSubscription(ctx, &notificationspb.CancelSubscriptionRequest{SubscriptionId: subscription.GetId()})
			return err
		},
		"ReadInbox": func(ctx context.Context) error {
			_, err := server.client.ReadInbox(ctx, &notificationspb.ReadInboxRequest{InboxId: subscription.GetInboxId()})
			return err
		},
		"AcknowledgeInbox": func(ctx context.Context) error {
			_, err := server.client.AcknowledgeInbox(ctx, &notificationspb.AcknowledgeInboxRequest{InboxId: subscription.GetInboxId(), ThroughCursor: 1})
			return err
		},
		"RetireInbox": func(ctx context.Context) error {
			_, err := server.client.RetireInbox(ctx, &notificationspb.RetireInboxRequest{InboxId: subscription.GetInboxId()})
			return err
		},
	}
}

func TestAnotherAccountSeesNoSubscriptionOrInboxOfTheCaller(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "private")
	server.record(t, subscription, 1)
	stranger := func(t *testing.T) context.Context { return bearing(t, otherSandboxToken) }

	subscriptions, err := server.client.ListSubscriptions(stranger(t), &notificationspb.ListSubscriptionsRequest{})
	if err != nil || len(subscriptions.GetSubscriptions()) != 0 {
		t.Errorf("listed %v, %v", subscriptions, err)
	}
	inboxes, err := server.client.ListInboxes(stranger(t), &notificationspb.ListInboxesRequest{})
	if err != nil || len(inboxes.GetInboxes()) != 0 {
		t.Errorf("listed %v, %v", inboxes, err)
	}
	for name, call := range ownedCalls(server, subscription) {
		t.Run(name, func(t *testing.T) { requireCode(t, call(stranger(t)), codes.NotFound) })
	}
	// None of it touched the owner's.
	read, err := server.client.ReadInbox(bearing(t, sandboxToken), &notificationspb.ReadInboxRequest{InboxId: subscription.GetInboxId()})
	if err != nil || len(read.GetEntries()) != 1 || read.GetInbox().GetAcknowledged() != 0 || read.GetInbox().GetRetired() {
		t.Errorf("read = %v, %v", read, err)
	}
}

func TestASandboxRecreatedUnderTheNameSeesNoneOfItsPredecessorsInboxes(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "predecessor")
	server.record(t, subscription, 1)
	server.recreateSandbox(t)

	subscriptions, err := server.client.ListSubscriptions(bearing(t, sandboxToken), &notificationspb.ListSubscriptionsRequest{})
	if err != nil || len(subscriptions.GetSubscriptions()) != 0 {
		t.Errorf("listed %v, %v", subscriptions, err)
	}
	inboxes, err := server.client.ListInboxes(bearing(t, sandboxToken), &notificationspb.ListInboxesRequest{})
	if err != nil || len(inboxes.GetInboxes()) != 0 {
		t.Errorf("listed %v, %v", inboxes, err)
	}
	for name, call := range ownedCalls(server, subscription) {
		t.Run(name, func(t *testing.T) { requireCode(t, call(bearing(t, sandboxToken)), codes.NotFound) })
	}

	// What the successor opens is its own.
	request := createRequest("successor")
	request.DestinationRef.Uid = successorUID
	created, err := server.client.CreateSubscription(bearing(t, sandboxToken), request)
	if err != nil {
		t.Fatal(err)
	}
	inboxes, err = server.client.ListInboxes(bearing(t, sandboxToken), &notificationspb.ListInboxesRequest{})
	if err != nil || len(inboxes.GetInboxes()) != 1 || inboxes.GetInboxes()[0].GetId() != created.GetSubscription().GetInboxId() {
		t.Errorf("listed %v, %v; want only the successor's inbox", inboxes, err)
	}
}

func TestAnAgentReadsAcknowledgesAndRetiresItsInbox(t *testing.T) {
	server := newServer(t)
	subscription := server.subscribe(t, "inbox")
	server.record(t, subscription, 3)
	inboxID := subscription.GetInboxId()
	read := func(request *notificationspb.ReadInboxRequest) (*notificationspb.ReadInboxResponse, error) {
		request.InboxId = inboxID
		return server.client.ReadInbox(bearing(t, sandboxToken), request)
	}

	page, err := read(&notificationspb.ReadInboxRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if len(page.GetEntries()) != 3 || page.GetInbox().GetLastCursor() != 3 || page.GetNotice() != nil {
		t.Fatalf("page = %v", page)
	}
	first := page.GetEntries()[0]
	wantEvent := &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_Actions{Actions: &notificationspb.ActionsEvent{
		RequestId: subscription.GetSource().GetActions().GetRequestId(), Sequence: 1,
	}}}
	if first.GetCursor() != 1 || !proto.Equal(first.GetEvent(), wantEvent) || first.GetPayload().GetFields()["state"].GetStringValue() != "decision_pending" ||
		len(first.GetSubscriptionIds()) != 1 || first.GetSubscriptionIds()[0] != subscription.GetId() {
		t.Errorf("first entry = %v", first)
	}

	limited, err := read(&notificationspb.ReadInboxRequest{Limit: proto.Int32(2)})
	if err != nil || len(limited.GetEntries()) != 2 {
		t.Fatalf("limited = %v, %v", limited, err)
	}
	rest, err := read(&notificationspb.ReadInboxRequest{AfterCursor: 2})
	if err != nil || len(rest.GetEntries()) != 1 || rest.GetEntries()[0].GetCursor() != 3 {
		t.Fatalf("rest = %v, %v", rest, err)
	}
	for name, request := range map[string]*notificationspb.ReadInboxRequest{
		"no limit":       {Limit: proto.Int32(0)},
		"too many":       {Limit: proto.Int32(129)},
		"negative start": {AfterCursor: -1},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := read(request)
			requireCode(t, err, codes.InvalidArgument)
		})
	}
	_, err = server.client.ReadInbox(bearing(t, sandboxToken), &notificationspb.ReadInboxRequest{InboxId: "inbox"})
	requireCode(t, err, codes.InvalidArgument)
	if again, err := read(&notificationspb.ReadInboxRequest{}); err != nil || again.GetInbox().GetAcknowledged() != 0 {
		t.Errorf("reading acknowledged: %v, %v", again, err)
	}

	acknowledge := func(through int64) (*notificationspb.AcknowledgeInboxResponse, error) {
		return server.client.AcknowledgeInbox(bearing(t, sandboxToken), &notificationspb.AcknowledgeInboxRequest{InboxId: inboxID, ThroughCursor: through})
	}
	if acknowledged, err := acknowledge(2); err != nil || acknowledged.GetInbox().GetAcknowledged() != 2 {
		t.Fatalf("acknowledged = %v, %v", acknowledged, err)
	}
	if lower, err := acknowledge(1); err != nil || lower.GetInbox().GetAcknowledged() != 2 {
		t.Errorf("an acknowledgement moved backwards: %v, %v", lower, err)
	}
	_, err = acknowledge(4)
	requireCode(t, err, codes.OutOfRange)
	_, err = acknowledge(-1)
	requireCode(t, err, codes.InvalidArgument)

	retire := func() error {
		_, err := server.client.RetireInbox(bearing(t, sandboxToken), &notificationspb.RetireInboxRequest{InboxId: inboxID})
		return err
	}
	if err := retire(); err != nil {
		t.Fatal(err)
	}
	if err := retire(); err != nil {
		t.Errorf("retiring a retired inbox: %v", err)
	}
	retired, err := read(&notificationspb.ReadInboxRequest{})
	if err != nil || !retired.GetInbox().GetRetired() || len(retired.GetEntries()) != 3 {
		t.Errorf("a retired inbox is read: %v, %v", retired, err)
	}
	got, err := server.client.GetSubscription(bearing(t, sandboxToken), &notificationspb.GetSubscriptionRequest{SubscriptionId: subscription.GetId()})
	if err != nil || !got.GetSubscription().GetCancelled() {
		t.Errorf("retiring cancels the subscriptions: %v, %v", got, err)
	}
	// A retired inbox takes no new subscription for its Session, and none of its subscriptions changes.
	_, err = server.client.CreateSubscription(bearing(t, sandboxToken), createRequest("after-retire"))
	requireCode(t, err, codes.FailedPrecondition)
	_, err = server.client.RenewSubscription(bearing(t, sandboxToken), &notificationspb.RenewSubscriptionRequest{
		SubscriptionId: subscription.GetId(), Version: got.GetSubscription().GetVersion(),
	})
	requireCode(t, err, codes.FailedPrecondition)
}
