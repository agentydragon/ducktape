package main

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
)

const subscriptionID = "00000000-0000-0000-0000-0000000000b1"

// notificationsServer is the Notification Service's gRPC API with canned answers. Every CLI call reaches it
// through the real gRPC-Gateway facade, so the routes, JSON and error bodies are the service's own.
type notificationsServer struct {
	notificationspb.UnimplementedNotificationsServer

	create func(*notificationspb.CreateSubscriptionRequest) (*notificationspb.Subscription, error)
	list   func(*notificationspb.ListSubscriptionsRequest) ([]*notificationspb.Subscription, error)
	get    func(*notificationspb.GetSubscriptionRequest) (*notificationspb.Subscription, error)
	renew  func(*notificationspb.RenewSubscriptionRequest) (*notificationspb.Subscription, error)
	cancel func(*notificationspb.CancelSubscriptionRequest) (*notificationspb.Subscription, error)

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

// serveNotifications runs the servicer behind the gateway and returns the facade's base URL.
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
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	handler, err := facade.NewHandler(ctx, listener.Addr().String(), notificationspb.RegisterNotificationsHandlerFromEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	facadeServer := httptest.NewServer(handler)
	t.Cleanup(facadeServer.Close)
	return facadeServer.URL
}

func pullRequestSubscription(version int64, cancelled bool) *notificationspb.Subscription {
	return &notificationspb.Subscription{
		Id: subscriptionID, InboxId: "00000000-0000-0000-0000-0000000000b2", IdempotencyKey: "k", Version: version,
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
	if ref := got.GetDestinationRef(); ref.GetNamespace() != "test-ns" || ref.GetName() != "test-sandbox" || ref.GetUid() != "test-uid" || got.GetSessionId() != "test-session" {
		t.Errorf("destination = %v %q, want the environment's", ref, got.GetSessionId())
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

func TestSubscribeDestinationFlagWinsOverTheEnvironment(t *testing.T) {
	servicer := answering(pullRequestSubscription(1, false))
	env := notificationsEnv(serveNotifications(t, servicer))
	flag := `{"destination_ref":{"namespace":"n","name":"s","uid":"u"},"session_id":"from-flag"}`
	invoke(env, subscribeArgs("--destination", flag)...)
	delete(env, "AGENTPLANE_DESTINATION")
	invoke(env, subscribeArgs("--destination", flag)...)
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
	if code != 1 || stdout != "" || got.Code != "FAILED_PRECONDITION" || got.HTTPStatus == nil || *got.HTTPStatus != http.StatusBadRequest || got.Message != "inbox is retired" {
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

func TestDestinationIsStrictAboutItsFields(t *testing.T) {
	servicer := answering(pullRequestSubscription(1, false))
	env := notificationsEnv(serveNotifications(t, servicer))
	for _, destination := range []string{
		`{"destination_ref":{"namespace":"n","name":"s","uid":"u"},"session_id":"x","source":{}}`,
		`{"destination_ref":{"namespace":"n","name":"s"},"session_id":"x"}`,
		`{"destination_ref":{"namespace":"n","name":"s","uid":"u"}}`,
		`not json`,
	} {
		if code, stdout, _ := invoke(env, subscribeArgs("--destination", destination)...); code != 2 || stdout != "" {
			t.Errorf("destination %s: exit %d, stdout %q; want exit 2", destination, code, stdout)
		}
	}
	if sent := received[*notificationspb.CreateSubscriptionRequest](servicer); len(sent) != 0 {
		t.Errorf("%d creations after bad destinations, want none", len(sent))
	}
}

func TestEgressRulesPassThroughAndProxyRefusalsAreAttributed(t *testing.T) {
	rules := `{"subject":{"namespace":"test-ns","name":"test-sa"},"policies":[{"name":"basic","rules":[{"hosts":["api.example.com"],"methods":{"values":["GET"]},"paths":null,"credential":null}]}]}`
	var refuse atomic.Bool
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		if refuse.Load() {
			w.Header().Set(egressHeader, "denied; reason=no-rule")
			reply(w, 403, "")
			return
		}
		reply(w, 200, rules)
	})
	code, stdout, _ := invoke(egressEnv(f.URL), "egress", "rules", "--json")
	if code != 0 || strings.TrimSpace(stdout) != rules {
		t.Errorf("exit %d, stdout %q; want the rules as served", code, stdout)
	}
	if code, stdout, _ := invoke(egressEnv(f.URL), "egress", "rules"); code != 0 || !strings.Contains(stdout, "api.example.com") {
		t.Errorf("text mode: exit %d, stdout %q", code, stdout)
	}
	refuse.Store(true)
	code, _, stderr := invoke(egressEnv(f.URL), "egress", "rules", "--json")
	if got := errorOf(t, stderr); code != 1 || got.Source != sourceEgress || got.Reason == nil || *got.Reason != "no-rule" {
		t.Errorf("exit %d, envelope %+v; want exit 1 attributed to the egress proxy", code, got)
	}
}
