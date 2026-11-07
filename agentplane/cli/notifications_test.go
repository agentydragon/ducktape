package main

import (
	"context"
	"fmt"
	"net"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
)

const subscriptionID = "00000000-0000-0000-0000-0000000000b1"

func TestNotificationsGuideIsAvailableWithoutServiceConfiguration(t *testing.T) {
	code, stdout, stderr := invoke(nil, "notifications", "guide", "inbox")
	if code != 0 || !strings.Contains(stdout, "acknowledge") {
		t.Fatalf("guide: exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
}

func TestSubscribeCreateAcceptsActionSourceAsProtoJSON(t *testing.T) {
	server := answering(pullRequestSubscription(1, false))
	cli := newCLI(notificationsEnv(serveNotifications(t, server)), `{"sessionId":"session-1","idempotencyKey":"follow-1","source":{"actions":{"requestId":"action-1","afterSequence":7}}}`)
	if code := cli.run("notifications", "subscribe", "create", "--request-file", "-"); code != 0 {
		t.Fatalf("create: exit %d: %s", code, cli.stderr.String())
	}
	requests := received[*notificationspb.CreateSubscriptionRequest](server)
	if len(requests) != 1 || requests[0].GetSessionId() != "session-1" || requests[0].GetIdempotencyKey() != "follow-1" || requests[0].GetSource().GetActions().GetRequestId() != "action-1" || requests[0].GetSource().GetActions().GetAfterSequence() != 7 {
		t.Fatalf("create requests: %v", requests)
	}
}

// notificationsServer is the Notification Service's gRPC API with canned answers. Every CLI call reaches it
// directly over gRPC, preserving the service's typed requests and status codes.
type notificationsServer struct {
	notificationspb.UnimplementedNotificationsServer

	create func(*notificationspb.CreateSubscriptionRequest) (*notificationspb.Subscription, error)
	list   func(*notificationspb.ListSubscriptionsRequest) ([]*notificationspb.Subscription, error)
	get    func(*notificationspb.GetSubscriptionRequest) (*notificationspb.Subscription, error)
	renew  func(*notificationspb.RenewSubscriptionRequest) (*notificationspb.Subscription, error)
	cancel func(*notificationspb.CancelSubscriptionRequest) (*notificationspb.Subscription, error)
	send   func(*notificationspb.SendMessageRequest) (*notificationspb.Message, error)
	lookup func(*notificationspb.GetMessageRequest) (*notificationspb.Message, error)

	mu            sync.Mutex
	calls         []proto.Message
	authorization []string
}

func (s *notificationsServer) record(ctx context.Context, request proto.Message) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls = append(s.calls, request)
	s.authorization = append(s.authorization, metadata.ValueFromIncomingContext(ctx, "authorization")...)
}

// received are the recorded requests of type T, oldest first.
func received[T proto.Message](s *notificationsServer) []T {
	s.mu.Lock()
	defer s.mu.Unlock()
	var matching []T
	for _, call := range s.calls {
		if request, ok := call.(T); ok {
			matching = append(matching, request)
		}
	}
	return matching
}

func (s *notificationsServer) CreateSubscription(ctx context.Context, request *notificationspb.CreateSubscriptionRequest) (*notificationspb.CreateSubscriptionResponse, error) {
	s.record(ctx, request)
	subscription, err := s.create(request)
	return &notificationspb.CreateSubscriptionResponse{Subscription: subscription}, err
}

func (s *notificationsServer) ListSubscriptions(ctx context.Context, request *notificationspb.ListSubscriptionsRequest) (*notificationspb.ListSubscriptionsResponse, error) {
	s.record(ctx, request)
	subscriptions, err := s.list(request)
	return &notificationspb.ListSubscriptionsResponse{Subscriptions: subscriptions}, err
}

func (s *notificationsServer) GetSubscription(ctx context.Context, request *notificationspb.GetSubscriptionRequest) (*notificationspb.GetSubscriptionResponse, error) {
	s.record(ctx, request)
	subscription, err := s.get(request)
	return &notificationspb.GetSubscriptionResponse{Subscription: subscription}, err
}

func (s *notificationsServer) RenewSubscription(ctx context.Context, request *notificationspb.RenewSubscriptionRequest) (*notificationspb.RenewSubscriptionResponse, error) {
	s.record(ctx, request)
	subscription, err := s.renew(request)
	return &notificationspb.RenewSubscriptionResponse{Subscription: subscription}, err
}

func (s *notificationsServer) CancelSubscription(ctx context.Context, request *notificationspb.CancelSubscriptionRequest) (*notificationspb.CancelSubscriptionResponse, error) {
	s.record(ctx, request)
	subscription, err := s.cancel(request)
	return &notificationspb.CancelSubscriptionResponse{Subscription: subscription}, err
}

func (s *notificationsServer) SendMessage(ctx context.Context, request *notificationspb.SendMessageRequest) (*notificationspb.SendMessageResponse, error) {
	s.record(ctx, request)
	message, err := s.send(request)
	return &notificationspb.SendMessageResponse{Message: message}, err
}

func (s *notificationsServer) GetMessage(ctx context.Context, request *notificationspb.GetMessageRequest) (*notificationspb.GetMessageResponse, error) {
	s.record(ctx, request)
	message, err := s.lookup(request)
	return &notificationspb.GetMessageResponse{Message: message}, err
}

func (s *notificationsServer) ReadInbox(ctx context.Context, request *notificationspb.ReadInboxRequest) (*notificationspb.ReadInboxResponse, error) {
	s.record(ctx, request)
	return &notificationspb.ReadInboxResponse{Inbox: &notificationspb.Inbox{Id: "inbox", SessionId: request.GetSessionId()}}, nil
}

func (s *notificationsServer) AcknowledgeInbox(ctx context.Context, request *notificationspb.AcknowledgeInboxRequest) (*notificationspb.AcknowledgeInboxResponse, error) {
	s.record(ctx, request)
	return &notificationspb.AcknowledgeInboxResponse{Inbox: &notificationspb.Inbox{Id: "inbox", SessionId: request.GetSessionId(), Acknowledged: request.GetThroughCursor()}}, nil
}

func TestInboxCommandsAddressTheSession(t *testing.T) {
	s := answering(pullRequestSubscription(1, false))
	env := notificationsEnv(serveNotifications(t, s))
	if code, _, stderr := invoke(env, "notifications", "inbox", "read", "session-1", "--after", "2", "--limit", "5"); code != 0 {
		t.Fatalf("read: %d %s", code, stderr)
	}
	if code, _, stderr := invoke(env, "notifications", "inbox", "ack", "session-1", "--through", "4"); code != 0 {
		t.Fatalf("ack: %d %s", code, stderr)
	}
	reads := received[*notificationspb.ReadInboxRequest](s)
	acks := received[*notificationspb.AcknowledgeInboxRequest](s)
	if len(reads) != 1 || reads[0].GetSessionId() != "session-1" || reads[0].GetAfterCursor() != 2 || reads[0].GetLimit() != 5 {
		t.Errorf("reads: %v", reads)
	}
	if len(acks) != 1 || acks[0].GetSessionId() != "session-1" || acks[0].GetThroughCursor() != 4 {
		t.Errorf("acks: %v", acks)
	}
}

// serveNotifications runs the Notification gRPC servicer.
func serveNotifications(t *testing.T, servicer *notificationsServer) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	notificationspb.RegisterNotificationsServer(server, servicer)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	return "http://" + listener.Addr().String()
}

func pullRequestSubscription(version int64, cancelled bool) *notificationspb.Subscription {
	return &notificationspb.Subscription{
		Id: subscriptionID, InboxId: "00000000-0000-0000-0000-0000000000b2", IdempotencyKey: proto.String("k"), Version: proto.Int64(version),
		Cancelled: cancelled, ExpiresAt: timestamppb.New(time.Date(2026, 1, 8, 0, 0, 0, 0, time.UTC)),
		Source: &notificationspb.Source{Provider: &notificationspb.Source_Github{Github: &notificationspb.GitHubSource{
			Repository: "owner/repo",
			Subject: &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_PullRequest{
				PullRequest: &notificationspb.PullRequestSubject{Number: 7},
			}},
		}}},
	}
}

// answering is a servicer whose every subscription RPC answers with subscription.
func answering(subscription *notificationspb.Subscription) *notificationsServer {
	return &notificationsServer{
		create: func(*notificationspb.CreateSubscriptionRequest) (*notificationspb.Subscription, error) {
			return subscription, nil
		},
		get: func(*notificationspb.GetSubscriptionRequest) (*notificationspb.Subscription, error) {
			return subscription, nil
		},
		renew: func(*notificationspb.RenewSubscriptionRequest) (*notificationspb.Subscription, error) {
			return subscription, nil
		},
		cancel: func(*notificationspb.CancelSubscriptionRequest) (*notificationspb.Subscription, error) {
			return subscription, nil
		},
		list: func(*notificationspb.ListSubscriptionsRequest) ([]*notificationspb.Subscription, error) {
			return []*notificationspb.Subscription{subscription}, nil
		},
	}
}

// failing is a servicer whose every subscription RPC fails with err.
func failing(err error) *notificationsServer {
	return &notificationsServer{
		create: func(*notificationspb.CreateSubscriptionRequest) (*notificationspb.Subscription, error) {
			return nil, err
		},
		get: func(*notificationspb.GetSubscriptionRequest) (*notificationspb.Subscription, error) { return nil, err },
		renew: func(*notificationspb.RenewSubscriptionRequest) (*notificationspb.Subscription, error) {
			return nil, err
		},
		cancel: func(*notificationspb.CancelSubscriptionRequest) (*notificationspb.Subscription, error) {
			return nil, err
		},
		list: func(*notificationspb.ListSubscriptionsRequest) ([]*notificationspb.Subscription, error) {
			return nil, err
		},
	}
}

func subscribeArgs(extra ...string) []string {
	return append([]string{"notifications", "subscribe", "github", "pr", "owner/repo#7"}, extra...)
}

func TestSubscribeToAPullRequest(t *testing.T) {
	servicer := answering(pullRequestSubscription(1, false))
	code, stdout, stderr := invoke(notificationsEnv(serveNotifications(t, servicer)), append(subscribeArgs(), "--json")...)
	sent := received[*notificationspb.CreateSubscriptionRequest](servicer)
	if code != 0 || len(sent) != 1 || decodeObject(t, stdout)["id"] != subscriptionID {
		t.Fatalf("exit %d, %d creations, stdout %q, stderr %q", code, len(sent), stdout, stderr)
	}
	got := sent[0]
	if got.GetSessionId() != "test-session" {
		t.Errorf("Session = %q, want the environment's", got.GetSessionId())
	}
	github := got.GetSource().GetGithub()
	if github.GetRepository() != "owner/repo" || github.GetSubject().GetPullRequest().GetNumber() != 7 {
		t.Errorf("source = %v", got.GetSource())
	}
	if len(github.GetEvents()) != 0 || got.LifetimeDays != nil || got.GetIdempotencyKey() == "" {
		t.Errorf("events %v, lifetime %v, key %q; want default events and lifetime and a derived key", github.GetEvents(), got.LifetimeDays, got.GetIdempotencyKey())
	}
	if !slices.Equal(servicer.authorization, []string{"Bearer " + testCredential}) {
		t.Errorf("authorization = %v, want the configured placeholder as a bearer", servicer.authorization)
	}
}

func TestSubscribeDerivesItsKeyFromTheSource(t *testing.T) {
	servicer := answering(pullRequestSubscription(1, false))
	env := notificationsEnv(serveNotifications(t, servicer))
	for _, args := range [][]string{
		subscribeArgs(),
		subscribeArgs("--lifetime-days", "30"),
		subscribeArgs("--event", "issue_comment:created", "--event", "check_run:requested,completed"),
		subscribeArgs("--event", "check_run:completed,requested", "--event", "issue_comment:created"),
		{"notifications", "subscribe", "github", "pr", "owner/repo#8"},
		subscribeArgs("--key", "explicit"),
	} {
		invoke(env, args...)
	}
	var keys []string
	for _, sent := range received[*notificationspb.CreateSubscriptionRequest](servicer) {
		keys = append(keys, sent.GetIdempotencyKey())
	}
	if len(keys) != 6 {
		t.Fatalf("keys = %v, want 6", keys)
	}
	same := func(i, j int) bool { return keys[i] == keys[j] }
	if !same(0, 1) || !same(2, 3) || same(0, 2) || same(0, 4) || keys[5] != "explicit" {
		t.Errorf("keys = %v; want one per source: lifetime and event order do not matter, events and number do", keys)
	}
}

func TestSubscribeEventsReplaceTheDefaults(t *testing.T) {
	servicer := answering(pullRequestSubscription(1, false))
	invoke(notificationsEnv(serveNotifications(t, servicer)), subscribeArgs(
		"--event", "pull_request_review", "--event", "check_run:completed,requested", "--event", "pull_request_review", "--lifetime-days", "14",
	)...)
	sent := received[*notificationspb.CreateSubscriptionRequest](servicer)
	if len(sent) != 1 {
		t.Fatalf("%d creations, want 1", len(sent))
	}
	want := []*notificationspb.GitHubEventFilter{
		{Event: notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST_REVIEW},
		{Event: notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CHECK_RUN, Actions: []string{"completed", "requested"}},
	}
	events := sent[0].GetSource().GetGithub().GetEvents()
	if len(events) != len(want) || !proto.Equal(events[0], want[0]) || !proto.Equal(events[1], want[1]) || sent[0].GetLifetimeDays() != 14 {
		t.Errorf("events %v, lifetime %v; want %v and 14", events, sent[0].LifetimeDays, want)
	}
}

func TestMergeabilityEventFilterHasNoActionsAndInboxSummaryNamesItsPullRequest(t *testing.T) {
	filters, err := parseEvents([]string{"mergeability_changed"})
	if err != nil || len(filters) != 1 || filters[0].GetEvent() != notificationspb.GitHubEventName_GITHUB_EVENT_NAME_MERGEABILITY_CHANGED || len(filters[0].GetActions()) != 0 {
		t.Fatalf("filters = %v, error = %v", filters, err)
	}
	if _, err := parseEvents([]string{"mergeability_changed:completed"}); err == nil {
		t.Fatal("mergeability_changed accepted an action suffix")
	}
	line := inboxEntryLine(&notificationspb.InboxEntry{
		Cursor: 5,
		Event: &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
			GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
				PullRequestNumber: 7, Observation: &notificationspb.GitHubMergeabilityEvent_Sequence{Sequence: 3},
			},
		}},
	})
	if want := "entry 5  github PR #7 mergeability sequence 3"; line != want {
		t.Errorf("summary = %q, want %q", line, want)
	}
	snapshot := inboxEntryLine(&notificationspb.InboxEntry{
		Cursor: 6,
		Event: &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
			GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
				PullRequestNumber: 7, Observation: &notificationspb.GitHubMergeabilityEvent_SnapshotVersion{SnapshotVersion: 4},
			},
		}},
	})
	if want := "entry 6  github PR #7 mergeability snapshot version 4"; snapshot != want {
		t.Errorf("snapshot summary = %q, want %q", snapshot, want)
	}
}

func TestSubscribeOnlyToMergeabilityReplacesTheDefaultEvents(t *testing.T) {
	servicer := answering(pullRequestSubscription(1, false))
	invoke(notificationsEnv(serveNotifications(t, servicer)), subscribeArgs("--event", "mergeability_changed")...)
	sent := received[*notificationspb.CreateSubscriptionRequest](servicer)
	if len(sent) != 1 {
		t.Fatalf("%d creations, want 1", len(sent))
	}
	events := sent[0].GetSource().GetGithub().GetEvents()
	if len(events) != 1 || events[0].GetEvent() != notificationspb.GitHubEventName_GITHUB_EVENT_NAME_MERGEABILITY_CHANGED || len(events[0].GetActions()) != 0 {
		t.Errorf("events = %v, want only actionless mergeability_changed", events)
	}
}

func TestSubscribeSessionFlagWinsOverTheEnvironment(t *testing.T) {
	servicer := answering(pullRequestSubscription(1, false))
	env := notificationsEnv(serveNotifications(t, servicer))
	flag := "from-flag"
	invoke(env, subscribeArgs("--session", flag)...)
	delete(env, "AGENTPLANE_SESSION_ID")
	invoke(env, subscribeArgs("--session", flag)...)
	code, _, _ := invoke(env, subscribeArgs()...)
	sent := received[*notificationspb.CreateSubscriptionRequest](servicer)
	if len(sent) != 2 || sent[0].GetSessionId() != "from-flag" || sent[1].GetSessionId() != "from-flag" || code != 2 {
		t.Errorf("%d creations; want the flag used with or without the variable, and exit 2 without either (got %d)", len(sent), code)
	}
}

func TestSubscribeToACancelledKeyExitsConflict(t *testing.T) {
	code, stdout, stderr := invoke(notificationsEnv(serveNotifications(t, answering(pullRequestSubscription(3, true)))), append(subscribeArgs(), "--json")...)
	if got := errorOf(t, stderr); code != 4 || stdout != "" || got.Code != "ALREADY_EXISTS" || got.Source != sourceClient {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 4 and empty stdout", code, stdout, got)
	}
}

func TestSubscribeWithAChangedLifetimeExitsConflict(t *testing.T) {
	servicer := failing(status.Error(codes.AlreadyExists, "idempotency key reused with different arguments"))
	code, stdout, stderr := invoke(notificationsEnv(serveNotifications(t, servicer)), append(subscribeArgs(), "--json")...)
	if got := errorOf(t, stderr); code != 4 || stdout != "" || got.Code != "ALREADY_EXISTS" || got.Source != sourceService {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 4 ALREADY_EXISTS from the service", code, stdout, got)
	}
}

func TestSubscribeToARetiredInboxExitsRefusedNamingTheCode(t *testing.T) {
	servicer := failing(status.Error(codes.FailedPrecondition, "inbox is retired"))
	code, stdout, stderr := invoke(notificationsEnv(serveNotifications(t, servicer)), append(subscribeArgs(), "--json")...)
	got := errorOf(t, stderr)
	if code != 1 || stdout != "" || got.Code != "FAILED_PRECONDITION" || got.HTTPStatus != nil || got.Message != "inbox is retired" {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 1 naming FAILED_PRECONDITION, not the 400 it arrives as", code, stdout, got)
	}
}

func TestAGitHubRateLimitCarriesItsRetryDelay(t *testing.T) {
	limited, err := status.New(codes.Unavailable, "GitHub rate limit").WithDetails(&errdetails.RetryInfo{RetryDelay: durationpb.New(30 * time.Second)})
	if err != nil {
		t.Fatal(err)
	}
	env := notificationsEnv(serveNotifications(t, failing(limited.Err())))
	code, stdout, stderr := invoke(env, append(subscribeArgs(), "--json")...)
	got := errorOf(t, stderr)
	if code != 3 || stdout != "" || got.Code != "UNAVAILABLE" || got.RetryAfterSeconds == nil || *got.RetryAfterSeconds != 30 {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 3 UNAVAILABLE retrying after 30 seconds", code, stdout, got)
	}
	if _, _, text := invoke(env, subscribeArgs()...); !strings.Contains(text, "retry after 30s") {
		t.Errorf("text stderr %q does not say when to retry", text)
	}
}

func TestRenewPatchesWithTheVersionJustRead(t *testing.T) {
	servicer := answering(pullRequestSubscription(4, false))
	servicer.renew = func(*notificationspb.RenewSubscriptionRequest) (*notificationspb.Subscription, error) {
		return pullRequestSubscription(5, false), nil
	}
	code, stdout, _ := invoke(notificationsEnv(serveNotifications(t, servicer)), "notifications", "renew", subscriptionID, "--lifetime-days", "30", "--json")
	renewals := received[*notificationspb.RenewSubscriptionRequest](servicer)
	if code != 0 || len(renewals) != 1 || len(received[*notificationspb.GetSubscriptionRequest](servicer)) != 1 {
		t.Fatalf("exit %d, %d renewals, stdout %q", code, len(renewals), stdout)
	}
	if got := renewals[0]; got.GetSubscriptionId() != subscriptionID || got.GetVersion() != 4 || got.GetLifetimeDays() != 30 {
		t.Errorf("renewal = %v, want %s at version 4 for 30 days", got, subscriptionID)
	}
	if got := decodeObject(t, stdout); got["version"] != "5" {
		t.Errorf("stdout %v, want the renewed subscription at version 5 (int64 is a string in JSON)", got)
	}
}

func TestRenewOfAStaleVersionExitsConflict(t *testing.T) {
	servicer := answering(pullRequestSubscription(4, false))
	servicer.renew = func(*notificationspb.RenewSubscriptionRequest) (*notificationspb.Subscription, error) {
		return nil, status.Error(codes.Aborted, "version changed")
	}
	code, stdout, stderr := invoke(notificationsEnv(serveNotifications(t, servicer)), "notifications", "renew", subscriptionID, "--json")
	if got := errorOf(t, stderr); code != 4 || stdout != "" || got.Code != "ABORTED" {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 4 ABORTED", code, stdout, got)
	}
}

func TestCancelSubscriptionDeletesIt(t *testing.T) {
	servicer := answering(pullRequestSubscription(2, true))
	code, stdout, _ := invoke(notificationsEnv(serveNotifications(t, servicer)), "notifications", "cancel", subscriptionID, "--json")
	cancelled := received[*notificationspb.CancelSubscriptionRequest](servicer)
	if code != 0 || len(cancelled) != 1 || cancelled[0].GetSubscriptionId() != subscriptionID || decodeObject(t, stdout)["cancelled"] != true {
		t.Errorf("exit %d, cancellations %v, stdout %q", code, cancelled, stdout)
	}
}

func TestSubscriptionOfAnotherAccountExitsNotFound(t *testing.T) {
	servicer := failing(status.Error(codes.NotFound, "subscription not found"))
	if code, stdout, _ := invoke(notificationsEnv(serveNotifications(t, servicer)), "notifications", "subscription", subscriptionID); code != 5 || stdout != "" {
		t.Errorf("exit %d, stdout %q; want exit 5", code, stdout)
	}
}

func TestSubscriptionTextNamesItsSource(t *testing.T) {
	subscription := func(source *notificationspb.Source) *notificationspb.Subscription {
		shown := pullRequestSubscription(3, true)
		shown.Source = source
		return shown
	}
	github := func(subject *notificationspb.GitHubSubject) *notificationspb.Source {
		return &notificationspb.Source{Provider: &notificationspb.Source_Github{Github: &notificationspb.GitHubSource{Repository: "owner/repo", Subject: subject}}}
	}
	rateLimited := subscription(github(&notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_Issue{Issue: &notificationspb.IssueSubject{Number: 12}}}))
	rateLimited.ErrorKind = notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_RATE_LIMITED.Enum()
	for _, tc := range []struct {
		name         string
		subscription *notificationspb.Subscription
		want         []string
	}{
		{"pull request", pullRequestSubscription(3, false), []string{subscriptionID, "github owner/repo pull_request:7", "v3", "expires 2026-01-08T00:00:00Z"}},
		{"issue with a failure, cancelled", rateLimited, []string{"github owner/repo issue:12", "cancelled", "error rate_limited"}},
		{"branch", subscription(github(&notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_Branch{Branch: &notificationspb.BranchSubject{Name: "feature/x"}}})), []string{"branch:feature/x"}},
		{"commit", subscription(github(&notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_Commit{Commit: &notificationspb.CommitSubject{Sha: strings.Repeat("a", 40)}}})), []string{"commit:" + strings.Repeat("a", 40)}},
		{"action request", subscription(&notificationspb.Source{Provider: &notificationspb.Source_Actions{Actions: &notificationspb.ActionsSource{RequestId: "request-a"}}}), []string{"actions request-a"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			code, stdout, stderr := invoke(notificationsEnv(serveNotifications(t, answering(tc.subscription))), "notifications", "subscription", subscriptionID)
			if code != 0 {
				t.Fatalf("exit %d, stderr %q", code, stderr)
			}
			for _, want := range tc.want {
				if !strings.Contains(stdout, want) {
					t.Errorf("stdout %q does not contain %q", stdout, want)
				}
			}
		})
	}
}

func TestSubscriptionsFollowPagesOf128(t *testing.T) {
	const total = 300
	ids := make([]string, total)
	for i := range ids {
		ids[i] = fmt.Sprintf("00000000-0000-0000-0000-%012d", i+1)
	}
	servicer := &notificationsServer{list: func(request *notificationspb.ListSubscriptionsRequest) ([]*notificationspb.Subscription, error) {
		start := 0
		for i, id := range ids {
			if id == request.GetAfterId() {
				start = i + 1
			}
		}
		var page []*notificationspb.Subscription
		for _, id := range ids[start:min(start+128, total)] {
			subscription := pullRequestSubscription(1, false)
			subscription.Id = id
			page = append(page, subscription)
		}
		return page, nil
	}}
	env := notificationsEnv(serveNotifications(t, servicer))
	for _, tc := range []struct {
		args []string
		want int
	}{{[]string{"--limit", "0"}, total}, {nil, 50}, {[]string{"--limit", "130"}, 130}, {[]string{"--after", ids[99], "--limit", "0"}, total - 100}} {
		code, stdout, _ := invoke(env, append([]string{"notifications", "subscriptions", "--json"}, tc.args...)...)
		if got := jsonLines(t, stdout); code != 0 || len(got) != tc.want || got[0]["inboxId"] == nil {
			t.Errorf("%v: exit %d with %d subscriptions, want %d", tc.args, code, len(got), tc.want)
		}
	}
}

func TestSubscribeRequiresSessionID(t *testing.T) {
	servicer := answering(pullRequestSubscription(1, false))
	env := notificationsEnv(serveNotifications(t, servicer))
	delete(env, "AGENTPLANE_SESSION_ID")
	if code, stdout, _ := invoke(env, subscribeArgs()...); code != 2 || stdout != "" {
		t.Errorf("exit %d, stdout %q; want exit 2 without AGENTPLANE_SESSION_ID", code, stdout)
	}
	if sent := received[*notificationspb.CreateSubscriptionRequest](servicer); len(sent) != 0 {
		t.Errorf("%d creations after bad destinations, want none", len(sent))
	}
}

const messageID = "00000000-0000-0000-0000-0000000000c1"

// acceptedMessage is a message to the test recipient's Sandbox as the service pins it.
func acceptedMessage() *notificationspb.Message {
	return &notificationspb.Message{
		Id: messageID, DestinationRef: &notificationspb.DestinationRef{Namespace: "test-recipient-ns", Name: "test-recipient", Uid: "test-recipient-uid"},
		SessionId: "test-recipient-session", IdempotencyKey: "test-key", AcceptedAt: timestamppb.New(time.Date(2026, 1, 8, 0, 0, 0, 0, time.UTC)),
	}
}

// messaging is a servicer whose message RPCs answer with message, or fail with err.
func messaging(message *notificationspb.Message, err error) *notificationsServer {
	return &notificationsServer{
		send:   func(*notificationspb.SendMessageRequest) (*notificationspb.Message, error) { return message, err },
		lookup: func(*notificationspb.GetMessageRequest) (*notificationspb.Message, error) { return message, err },
	}
}

func sendArgs(extra ...string) []string {
	return append([]string{"notifications", "send", "test-recipient-session"}, extra...)
}

func TestSendAMessageFromThisSession(t *testing.T) {
	servicer := messaging(acceptedMessage(), nil)
	code, stdout, stderr := invoke(notificationsEnv(serveNotifications(t, servicer)), sendArgs("--body", "Please review the change.", "--json")...)
	sent := received[*notificationspb.SendMessageRequest](servicer)
	if code != 0 || len(sent) != 1 || decodeObject(t, stdout)["id"] != messageID {
		t.Fatalf("exit %d, %d sends, stdout %q, stderr %q", code, len(sent), stdout, stderr)
	}
	want := &notificationspb.SendMessageRequest{
		SessionId: "test-recipient-session", IdempotencyKey: sent[0].GetIdempotencyKey(), Body: "Please review the change.",
		FromSession: proto.String("test-session"),
	}
	if !proto.Equal(sent[0], want) {
		t.Errorf("sent %v, want %v: addressed by Session ID, from AGENTPLANE_SESSION_ID", sent[0], want)
	}
	// The generated key is on stderr before the POST, for a retry with --key.
	if got := decodeObject(t, stderr); sent[0].GetIdempotencyKey() == "" || got["idempotency_key"] != sent[0].GetIdempotencyKey() {
		t.Errorf("stderr %q, want the generated key %q", stderr, sent[0].GetIdempotencyKey())
	}
}

func TestSendTakesTheTextFromStdinAndItsOptionsFromFlags(t *testing.T) {
	servicer := messaging(acceptedMessage(), nil)
	env := notificationsEnv(serveNotifications(t, servicer))
	c := newCLI(env, "line one\nline two\n")
	code := c.run(sendArgs("--body-file", "-", "--from-session", "test-flag-session", "--key", "test-key")...)
	delete(env, "AGENTPLANE_SESSION_ID")
	invoke(env, sendArgs("--body", "test body", "--key", "test-key-2")...)
	sent := received[*notificationspb.SendMessageRequest](servicer)
	if code != 0 || len(sent) != 2 {
		t.Fatalf("exit %d, %d sends, stderr %q", code, len(sent), c.stderr.String())
	}
	if got := sent[0]; got.GetBody() != "line one\nline two\n" || got.GetSessionId() != "test-recipient-session" ||
		got.GetFromSession() != "test-flag-session" || got.GetIdempotencyKey() != "test-key" {
		t.Errorf("sent %v; want stdin as given, the flags' UID, Session and key", got)
	}
	if c.stderr.String() != "" {
		t.Errorf("stderr %q, want nothing when --key is given", c.stderr.String())
	}
	if sent[1].FromSession != nil {
		t.Errorf("from_session %q without AGENTPLANE_SESSION_ID, want none", sent[1].GetFromSession())
	}
}

func TestSendRefusesAMalformedCommandBeforeSending(t *testing.T) {
	servicer := messaging(acceptedMessage(), nil)
	env := notificationsEnv(serveNotifications(t, servicer))
	for name, args := range map[string][]string{
		"extra destination": {"notifications", "send", "test-ns/test-recipient", "test-recipient-session", "--body", "x"},
		"no text":           sendArgs(),
		"two texts":         sendArgs("--body", "x", "--body-file", "-"),
		"missing file":      sendArgs("--body-file", t.TempDir()+"/test-missing"),
	} {
		if code, stdout, _ := invoke(env, args...); code != 2 || stdout != "" {
			t.Errorf("%s: exit %d, stdout %q; want exit 2", name, code, stdout)
		}
	}
	if sent := received[*notificationspb.SendMessageRequest](servicer); len(sent) != 0 {
		t.Errorf("%d sends, want none", len(sent))
	}
}

func TestSendExitsWithWhatTheRefusalAsks(t *testing.T) {
	for _, tc := range []struct {
		code codes.Code
		name string
		exit int
	}{
		{codes.PermissionDenied, "PERMISSION_DENIED", 1},
		{codes.FailedPrecondition, "FAILED_PRECONDITION", 1},
		{codes.Unavailable, "UNAVAILABLE", 3},
		{codes.ResourceExhausted, "RESOURCE_EXHAUSTED", 3},
		{codes.AlreadyExists, "ALREADY_EXISTS", 4},
		{codes.NotFound, "NOT_FOUND", 5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			servicer := messaging(nil, status.Error(tc.code, "test refusal"))
			exit, stdout, stderr := invoke(notificationsEnv(serveNotifications(t, servicer)), sendArgs("--body", "x", "--key", "test-key", "--json")...)
			if got := errorOf(t, stderr); exit != tc.exit || stdout != "" || got.Code != tc.name {
				t.Errorf("exit %d, stdout %q, envelope %+v; want exit %d", exit, stdout, got, tc.exit)
			}
		})
	}
}

func TestMessageShowsWhatTheSenderCanFollow(t *testing.T) {
	message := acceptedMessage()
	message.Acknowledged = true
	servicer := messaging(message, nil)
	code, stdout, stderr := invoke(notificationsEnv(serveNotifications(t, servicer)), "notifications", "message", messageID)
	looked := received[*notificationspb.GetMessageRequest](servicer)
	if code != 0 || len(looked) != 1 || looked[0].GetMessageId() != messageID {
		t.Fatalf("exit %d, lookups %v, stderr %q", code, looked, stderr)
	}
	for _, want := range []string{messageID, "to test-recipient-ns/test-recipient session test-recipient-session", "accepted 2026-01-08T00:00:00Z", "acknowledged"} {
		if !strings.Contains(stdout, want) {
			t.Errorf("stdout %q does not contain %q", stdout, want)
		}
	}
	if strings.Contains(stdout, "covered") {
		t.Errorf("stdout %q says covered, which this message is not", stdout)
	}
}

func TestInboxSummaryNamesGitHubObservations(t *testing.T) {
	for _, event := range []struct {
		name notificationspb.GitHubEventName
		want string
	}{
		{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CHECK_RUN, "entry 7  github check_run observation sequence 12"},
		{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_STATUS, "entry 7  github status observation sequence 12"},
		{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST, "entry 7  github pull_request observation sequence 12"},
		{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST_REVIEW, "entry 7  github pull_request_review observation sequence 12"},
	} {
		line := inboxEntryLine(&notificationspb.InboxEntry{
			Cursor: 7,
			Event: &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubObservation{
				GithubObservation: &notificationspb.GitHubObservationEvent{
					RepositoryId: 22, Event: event.name, Sequence: 12,
				},
			}},
		})
		if line != event.want {
			t.Errorf("summary = %q, want %q", line, event.want)
		}
	}
}

func TestSubscriptionSummarySerializesGitHubStatus(t *testing.T) {
	raw := []byte(`{
		"id":"subscription",
		"source":{"github":{"repository":"owner/repo","subject":{"pullRequest":{"number":7}}}},
		"version":"1",
		"github":{
			"checkRuns":{"lastSuccessAt":"2026-10-27T10:00:00Z","errorKind":"SOURCE_FAILURE_KIND_RATE_LIMITED","error":"check runs rate limited","retryAt":"2026-10-27T10:02:00Z"},
			"commitStatuses":{"lastSuccessAt":"2026-10-27T11:00:00Z","errorKind":"SOURCE_FAILURE_KIND_ACCESS_DENIED","error":"statuses denied","retryAt":"2026-10-27T11:02:00Z"},
			"reviews":{"lastSuccessAt":"2026-10-27T12:00:00Z","errorKind":"SOURCE_FAILURE_KIND_UNAVAILABLE","error":"reviews unavailable","retryAt":"2026-10-27T12:02:00Z","refreshingUntil":"2026-10-27T12:03:00Z"}
		}
	}`)
	line, err := subscriptionLine(raw)
	if err != nil {
		t.Fatal(err)
	}
	_, encoded, found := strings.Cut(line, "github status ")
	if !found {
		t.Fatalf("summary %q does not include GitHub status", line)
	}
	var status notificationspb.GitHubStatus
	if err := protojson.Unmarshal([]byte(encoded), &status); err != nil {
		t.Fatal(err)
	}
	if status.GetCheckRuns().GetError() != "check runs rate limited" || status.GetCheckRuns().GetErrorKind() != notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_RATE_LIMITED || status.GetCheckRuns().GetRetryAt().AsTime().UTC().Format(time.RFC3339) != "2026-10-27T10:02:00Z" {
		t.Fatalf("summary did not preserve check-run status: %v", &status)
	}
	if status.GetCommitStatuses().GetError() != "statuses denied" || status.GetCommitStatuses().GetErrorKind() != notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_ACCESS_DENIED || status.GetCommitStatuses().GetRetryAt().AsTime().UTC().Format(time.RFC3339) != "2026-10-27T11:02:00Z" {
		t.Fatalf("summary did not preserve GitHub status: %v", &status)
	}
	if status.GetReviews() == nil || status.GetReviews().GetError() != "reviews unavailable" || status.GetReviews().GetErrorKind() != notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_UNAVAILABLE ||
		status.GetReviews().GetRetryAt().AsTime().UTC().Format(time.RFC3339) != "2026-10-27T12:02:00Z" ||
		status.GetReviews().GetRefreshingUntil().AsTime().UTC().Format(time.RFC3339) != "2026-10-27T12:03:00Z" {
		t.Fatalf("summary did not preserve review status: %v", &status)
	}
}
