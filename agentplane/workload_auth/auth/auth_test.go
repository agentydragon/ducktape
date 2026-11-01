package auth_test

import (
	"context"
	"errors"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	testAudience  = "test-dispatcher"
	testNamespace = "test-callers"
)

var (
	api *kubeapiserver.Server
	app = auth.Account{Namespace: testNamespace, Name: "test-app"}
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error {
		api = server
		return nil
	}, kubeapiserver.WithUserAudiences(testAudience)))
}

// workload is a new Pod running as account and a token for testAudience bound to it.
func workload(t *testing.T, account auth.Account) (*corev1.Pod, string) {
	t.Helper()
	pod, err := api.Pod(t.Context(), account)
	if err != nil {
		t.Fatal(err)
	}
	token, err := api.PodToken(t.Context(), pod, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	return pod, token
}

func newResolver(t *testing.T, reviews auth.TokenReviews, options ...auth.Option) *auth.Resolver {
	t.Helper()
	resolver, err := auth.NewResolver(reviews, testAudience, []string{testNamespace}, options...)
	if err != nil {
		t.Fatal(err)
	}
	return resolver
}

func incoming(token string) context.Context {
	return metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+token))
}

var errAPIServerDown = errors.New("test API server down")

// outage is an API server that cannot be reached while down: a review fails in transport, which no
// running API server does. Up, it passes reviews to the real one.
type outage struct {
	auth.TokenReviews
	down atomic.Bool
}

func (o *outage) Create(ctx context.Context, review *authenticationv1.TokenReview, options metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	if o.down.Load() {
		return nil, errAPIServerDown
	}
	return o.TokenReviews.Create(ctx, review, options)
}

// heldReviews holds each review in flight until released, then passes it to the API server, which
// answers too quickly for a test to act while a review is pending.
type heldReviews struct {
	auth.TokenReviews
	release chan struct{}
	// started is closed when the first review starts.
	started chan struct{}
	once    sync.Once
	calls   atomic.Int32
}

func newHeldReviews() *heldReviews {
	return &heldReviews{TokenReviews: api.TokenReviews(), release: make(chan struct{}), started: make(chan struct{})}
}

func (h *heldReviews) Create(ctx context.Context, review *authenticationv1.TokenReview, options metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	h.calls.Add(1)
	h.once.Do(func() { close(h.started) })
	select {
	case <-h.release:
		return h.TokenReviews.Create(ctx, review, options)
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// misansweredReviews passes each review to the API server and rewrites its answer into one the API
// server never gives.
type misansweredReviews struct {
	auth.TokenReviews
	rewrite func(*authenticationv1.TokenReviewStatus)
}

func (m misansweredReviews) Create(ctx context.Context, review *authenticationv1.TokenReview, options metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	answer, err := m.TokenReviews.Create(ctx, review, options)
	if err != nil {
		return nil, err
	}
	m.rewrite(&answer.Status)
	return answer, nil
}

func TestResolveAndAuthorize(t *testing.T) {
	pod, token := workload(t, app)
	account, err := api.ServiceAccount(t.Context(), app)
	if err != nil {
		t.Fatal(err)
	}
	resolver := newResolver(t, api.TokenReviews())
	principal, err := resolver.RequireAccount(incoming(token), map[auth.Account]struct{}{app: {}})
	if err != nil {
		t.Fatal(err)
	}
	want := auth.Principal{Account: app, ServiceAccountUID: string(account.UID), PodName: pod.Name, PodUID: string(pod.UID)}
	if principal != want {
		t.Fatalf("principal %+v, want %+v", principal, want)
	}
	_, err = resolver.RequireAccount(incoming(token), map[auth.Account]struct{}{{Namespace: testNamespace, Name: "test-notifications"}: {}})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("wrong account: %v", err)
	}
}

func TestAccountReadsNamespaceSlashName(t *testing.T) {
	var account auth.Account
	if err := account.UnmarshalText([]byte("test-namespace/test-app")); err != nil || account != (auth.Account{Namespace: "test-namespace", Name: "test-app"}) {
		t.Fatalf("namespace/name rejected: %+v, %v", account, err)
	}
	for _, malformed := range []string{"", "test-app", "/test-app", "test-namespace/", "test-namespace/test-app/extra"} {
		if err := new(auth.Account).UnmarshalText([]byte(malformed)); err == nil {
			t.Fatalf("%q accepted", malformed)
		}
	}
}

func TestRejectsInvalidWorkload(t *testing.T) {
	pod, _ := workload(t, app)
	otherAudience, err := api.PodToken(t.Context(), pod, "test-gateway")
	if err != nil {
		t.Fatal(err)
	}
	unbound, err := api.UnboundToken(t.Context(), app, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	_, elsewhere := workload(t, auth.Account{Namespace: "test-elsewhere", Name: app.Name})
	for name, c := range map[string]struct {
		bearer string
		// reviewed is whether the API server authenticates the bearer, which the resolver then refuses.
		reviewed bool
	}{
		"unauthenticated": {"test-not-a-token", false},
		"wrong audience":  {otherAudience, false},
		"user token":      {api.UserToken, true},
		"other namespace": {elsewhere, true},
		"no Pod":          {unbound, true},
	} {
		t.Run(name, func(t *testing.T) {
			review, err := api.TokenReviews().Create(t.Context(), &authenticationv1.TokenReview{
				Spec: authenticationv1.TokenReviewSpec{Token: c.bearer, Audiences: []string{testAudience}},
			}, metav1.CreateOptions{})
			if err != nil || review.Status.Authenticated != c.reviewed {
				t.Fatalf("the API server's review: %+v, %v", review, err)
			}
			if _, err := newResolver(t, api.TokenReviews()).Resolve(incoming(c.bearer)); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("got %v", err)
			}
		})
	}
}

func TestRejectsAnAnswerTheAPIServerNeverGives(t *testing.T) {
	_, token := workload(t, app)
	if _, err := newResolver(t, api.TokenReviews()).ResolveToken(t.Context(), token); err != nil {
		t.Fatalf("the API server's own answer: %v", err)
	}
	for name, rewrite := range map[string]func(*authenticationv1.TokenReviewStatus){
		// The API server authenticates a token only for audiences the review names.
		"authenticated for another audience": func(s *authenticationv1.TokenReviewStatus) { s.Audiences = []string{"test-gateway"} },
		// It names the one Pod a token is bound to.
		"two Pod names": func(s *authenticationv1.TokenReviewStatus) {
			s.User.Extra["authentication.kubernetes.io/pod-name"] = append(s.User.Extra["authentication.kubernetes.io/pod-name"], "test-other-pod")
		},
		// It names the UID of every ServiceAccount it proves.
		"no ServiceAccount UID": func(s *authenticationv1.TokenReviewStatus) { s.User.UID = "" },
	} {
		t.Run(name, func(t *testing.T) {
			resolver := newResolver(t, misansweredReviews{TokenReviews: api.TokenReviews(), rewrite: rewrite})
			if _, err := resolver.Resolve(incoming(token)); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("got %v", err)
			}
		})
	}
}

func TestMetadataAndReviewFailure(t *testing.T) {
	unreachable := &outage{}
	unreachable.down.Store(true)
	resolver := newResolver(t, unreachable)
	for _, ctx := range []context.Context{
		context.Background(),
		metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "token")),
		metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer a", "authorization", "Bearer b")),
	} {
		_, err := resolver.Resolve(ctx)
		if status.Code(err) != codes.Unauthenticated {
			t.Fatalf("got %v", err)
		}
	}
	_, err := resolver.Resolve(incoming("test-token"))
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("got %v", err)
	}
	if _, err = resolver.ResolveToken(context.Background(), "test-token"); !errors.Is(err, auth.ErrUnavailable) || !errors.Is(err, errAPIServerDown) {
		t.Fatalf("a failed review is Unavailable and keeps its cause: %v", err)
	}
}

func TestAResolverNeedsAReviewerAnAudienceAndANamespace(t *testing.T) {
	if _, err := auth.NewResolver(api.TokenReviews(), testAudience, nil); err == nil {
		t.Fatal("a resolver that serves no namespace was built")
	}
	if _, err := auth.NewResolver(api.TokenReviews(), "", []string{testNamespace}); err == nil {
		t.Fatal("a resolver with no audience was built")
	}
	if _, err := auth.NewResolver(nil, testAudience, []string{testNamespace}); err == nil {
		t.Fatal("a resolver with no reviewer was built")
	}
}

func TestAnAcceptanceIsReusedPerBearerAndLapsesWithItsTTL(t *testing.T) {
	reviews := &kubeapiserver.CountedReviews{TokenReviews: api.TokenReviews()}
	resolver := newResolver(t, reviews)
	short := newResolver(t, api.TokenReviews(), auth.WithVerdictTTL(time.Millisecond))
	pod, token := workload(t, app)
	_, other := workload(t, app)
	for range 3 {
		if _, err := resolver.ResolveToken(t.Context(), token); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := resolver.ResolveToken(t.Context(), other); err != nil {
		t.Fatal(err)
	}
	if got := reviews.Calls.Load(); got != 2 {
		t.Fatalf("each bearer is reviewed once while its acceptance is fresh, got %d reviews", got)
	}
	if _, err := short.ResolveToken(t.Context(), token); err != nil {
		t.Fatal(err)
	}

	if err := api.Revoke(t.Context(), pod, token, testAudience); err != nil {
		t.Fatal(err)
	}
	if _, err := resolver.ResolveToken(t.Context(), token); err != nil {
		t.Fatalf("a revocation is not seen while the acceptance is fresh: %v", err)
	}
	if _, err := short.ResolveToken(t.Context(), token); !errors.Is(err, auth.ErrInvalidBearer) {
		t.Fatalf("a revocation is seen once the TTL lapses: %v", err)
	}
}

func TestAZeroTTLReviewsEveryCall(t *testing.T) {
	_, token := workload(t, app)
	reviews := &kubeapiserver.CountedReviews{TokenReviews: api.TokenReviews()}
	resolver := newResolver(t, reviews, auth.WithVerdictTTL(0))
	for range 2 {
		if _, err := resolver.ResolveToken(t.Context(), token); err != nil {
			t.Fatal(err)
		}
	}
	if got := reviews.Calls.Load(); got != 2 {
		t.Fatalf("got %d reviews", got)
	}
}

func TestARejectionIsNotReused(t *testing.T) {
	reviews := &kubeapiserver.CountedReviews{TokenReviews: api.TokenReviews()}
	resolver := newResolver(t, reviews)
	for range 2 {
		if _, err := resolver.ResolveToken(t.Context(), "test-not-a-token"); !errors.Is(err, auth.ErrInvalidBearer) {
			t.Fatalf("got %v", err)
		}
	}
	if got := reviews.Calls.Load(); got != 2 {
		t.Fatalf("got %d reviews, want each call to ask again", got)
	}
}

func TestAFailedReviewIsNotReused(t *testing.T) {
	_, token := workload(t, app)
	unreachable := &outage{TokenReviews: api.TokenReviews()}
	unreachable.down.Store(true)
	reviews := &kubeapiserver.CountedReviews{TokenReviews: unreachable}
	resolver := newResolver(t, reviews)
	for range 2 {
		if _, err := resolver.ResolveToken(t.Context(), token); !errors.Is(err, auth.ErrUnavailable) {
			t.Fatalf("got %v", err)
		}
	}
	unreachable.down.Store(false)
	if _, err := resolver.ResolveToken(t.Context(), token); err != nil {
		t.Fatalf("the next call asks again: %v", err)
	}
	if got := reviews.Calls.Load(); got != 3 {
		t.Fatalf("got %d reviews", got)
	}
}

func TestACallerThatGivesUpDoesNotFailTheReviewItStarted(t *testing.T) {
	_, token := workload(t, app)
	reviews := newHeldReviews()
	resolver := newResolver(t, reviews)
	giveUp, cancel := context.WithCancel(context.Background())
	gaveUp := make(chan error)
	go func() {
		_, err := resolver.ResolveToken(giveUp, token)
		gaveUp <- err
	}()
	<-reviews.started
	cancel()
	if err := <-gaveUp; !errors.Is(err, auth.ErrUnavailable) || !errors.Is(err, context.Canceled) {
		t.Fatalf("a caller that gives up is told so: %v", err)
	}

	close(reviews.release)
	if _, err := resolver.ResolveToken(context.Background(), token); err != nil {
		t.Fatalf("the next caller gets the review the first one started: %v", err)
	}
	if got := reviews.calls.Load(); got != 1 {
		t.Fatalf("got %d reviews", got)
	}
}

func TestACallerWhoseContextEndedIsToldSoNotThatTheAPIIsDown(t *testing.T) {
	_, token := workload(t, app)
	reviews := newHeldReviews()
	t.Cleanup(func() { close(reviews.release) })
	resolver := newResolver(t, reviews)
	for want, end := range map[codes.Code]func() (context.Context, context.CancelFunc){
		codes.Canceled: func() (context.Context, context.CancelFunc) {
			ctx, cancel := context.WithCancel(incoming(token))
			cancel()
			return ctx, cancel
		},
		codes.DeadlineExceeded: func() (context.Context, context.CancelFunc) {
			return context.WithDeadline(incoming(token), time.Unix(0, 0))
		},
	} {
		ctx, cancel := end()
		defer cancel()
		if _, err := resolver.Resolve(ctx); status.Code(err) != want {
			t.Errorf("got %v, want %v", err, want)
		}
	}
}
