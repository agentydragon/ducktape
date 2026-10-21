package push

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/google/uuid"
)

// awaitBound only fails a test whose push never comes.
const awaitBound = 30 * time.Second

var (
	testOperator = store.OperatorPrincipal{Issuer: "https://issuer.test.example", Subject: "test-operator"}
	testCaller   = store.CallerPrincipal{Account: store.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"}}
)

// delivery is a Notifier's database, with the requests and subscriptions in it, and the push service it delivers to.
type delivery struct {
	url           string
	db            *sql.DB
	store         *store.Store
	subscriptions *Subscriptions
	identity      *Identity
	// applicationServerKey is the key the browsers subscribe with.
	applicationServerKey string
	pushes               *pushService
}

func newDelivery(t *testing.T) *delivery {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db := open(t, dsn)
	settings, applicationServerKey := testSettings(t)
	identity, err := NewIdentity(settings)
	if err != nil {
		t.Fatal(err)
	}
	return &delivery{
		url: dsn, db: db, store: store.New(db), subscriptions: NewSubscriptions(db), identity: identity,
		applicationServerKey: applicationServerKey, pushes: newPushService(t),
	}
}

func open(t *testing.T, dsn string) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

// notifier is a replica's Notifier: one of its own database handle.
func (d *delivery) notifier(t *testing.T, timing Timing) *Notifier {
	t.Helper()
	db := open(t, d.url)
	notifier, err := NewNotifier(db, store.New(db), d.identity, "https://app.test/", d.pushes.transport(), timing)
	if err != nil {
		t.Fatal(err)
	}
	return notifier
}

func (d *delivery) subscribe(t *testing.T, browsers ...browser) {
	t.Helper()
	for _, b := range browsers {
		if err := d.subscriptions.Save(t.Context(), testOperator, b.registration()); err != nil {
			t.Fatal(err)
		}
	}
}

// submit stores a request awaiting a Decision, or one a policy allowed at once when allowed is set.
func (d *delivery) submit(t *testing.T, key string, allowed bool) store.ActionRequest {
	t.Helper()
	admission := store.Admission{RequestID: uuid.New()}
	if allowed {
		admission.Vote = &store.ProviderVote{Provider: "test-policy", Verdict: store.VerdictAllow, ReasonCode: "test-allow"}
	}
	request, err := d.store.Submit(t.Context(), testCaller, store.ActionRequestInput{
		IdempotencyKey: key, Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments: json.RawMessage(`{}`), Title: "test title for " + key,
	}, admission)
	if err != nil {
		t.Fatal(err)
	}
	return request
}

func (d *delivery) deny(t *testing.T, request store.ActionRequest) {
	t.Helper()
	if _, _, err := d.store.Decide(t.Context(), request.ID, store.DecisionInput{
		Verdict: store.VerdictDeny, ExpectedVersion: request.Version, IdempotencyKey: "deny-" + request.IdempotencyKey,
	}, testOperator); err != nil {
		t.Fatal(err)
	}
}

func reconcile(t *testing.T, n *Notifier) {
	t.Helper()
	if err := n.reconcile(t.Context()); err != nil {
		t.Fatal(err)
	}
}

// one reads the one push the browser received since the last take.
func (d *delivery) one(t *testing.T, b browser) message {
	t.Helper()
	pushes := d.pushes.take(b)
	if len(pushes) != 1 {
		t.Fatalf("%s received %d pushes, want 1", b.endpoint, len(pushes))
	}
	return b.read(t, pushes[0], d.applicationServerKey)
}

func (d *delivery) none(t *testing.T, b browser) {
	t.Helper()
	if pushes := d.pushes.take(b); len(pushes) > 0 {
		t.Errorf("%s received %d pushes, want none", b.endpoint, len(pushes))
	}
}

func TestEachPassPushesTheNewestChangeOnceRatherThanOnePushPerRequest(t *testing.T) {
	d := newDelivery(t)
	b := newBrowser(t, "test-browser")
	d.subscribe(t, b)
	requests := []store.ActionRequest{d.submit(t, "test-first", false), d.submit(t, "test-second", false), d.submit(t, "test-third", false)}
	newest := requests[2]
	notifier := d.notifier(t, DefaultTiming())

	reconcile(t, notifier)
	want := message{
		Kind: "show", ActionID: newest.ID.String(), ActionGroup: "test-group", ActionName: "test-action", Version: newest.Version,
		URL: "https://app.test/#/actions/" + newest.ID.String(),
	}
	if got := d.one(t, b); got != want {
		t.Errorf("push %+v, want %+v", got, want)
	}
	reconcile(t, notifier)
	d.none(t, b)

	for _, request := range requests {
		d.deny(t, request)
	}
	reconcile(t, notifier)
	if got, want := d.one(t, b), (message{Kind: "retract", ActionID: newest.ID.String(), Outcome: "denied"}); got != want {
		t.Errorf("push %+v, want %+v", got, want)
	}
	reconcile(t, notifier)
	d.none(t, b)
}

func TestARequestThatArrivesWhileAnotherIsShownIsShownAndOneThatLeavesIsRetracted(t *testing.T) {
	d := newDelivery(t)
	b := newBrowser(t, "test-browser")
	d.subscribe(t, b)
	first := d.submit(t, "test-first", false)
	notifier := d.notifier(t, DefaultTiming())
	reconcile(t, notifier)
	d.one(t, b)

	second := d.submit(t, "test-second", false)
	reconcile(t, notifier)
	if got := d.one(t, b); got.Kind != "show" || got.ActionID != second.ID.String() {
		t.Errorf("push %+v, want the second request shown", got)
	}
	d.deny(t, first)
	reconcile(t, notifier)
	if got := d.one(t, b); got.Kind != "retract" || got.ActionID != first.ID.String() {
		t.Errorf("push %+v, want the first request retracted", got)
	}
}

func TestARequestAPolicyAllowedIsNeverPushed(t *testing.T) {
	d := newDelivery(t)
	b := newBrowser(t, "test-browser")
	d.subscribe(t, b)
	d.submit(t, "test-allowed", true)
	reconcile(t, d.notifier(t, DefaultTiming()))
	d.none(t, b)
}

func TestAPushTheServiceRefusedIsSentAgainOnTheNextPass(t *testing.T) {
	d := newDelivery(t)
	b := newBrowser(t, "test-browser")
	d.subscribe(t, b)
	request := d.submit(t, "test-pending", false)
	notifier := d.notifier(t, DefaultTiming())

	d.pushes.answering(func(string) int { return http.StatusInternalServerError })
	reconcile(t, notifier)
	d.one(t, b)
	d.pushes.answering(func(string) int { return http.StatusCreated })
	reconcile(t, notifier)
	if got := d.one(t, b); got.Kind != "show" || got.ActionID != request.ID.String() {
		t.Errorf("push %+v, want the request shown again", got)
	}
	reconcile(t, notifier)
	d.none(t, b)
}

func TestASubscriptionThePushServiceNoLongerKnowsIsDeletedAndAnnounced(t *testing.T) {
	for _, code := range []int{http.StatusNotFound, http.StatusGone} {
		t.Run(http.StatusText(code), func(t *testing.T) {
			d := newDelivery(t)
			gone, kept := newBrowser(t, "test-gone"), newBrowser(t, "test-kept")
			d.subscribe(t, gone, kept)
			d.submit(t, "test-pending", false)
			hub, err := wakeups.Listen(t.Context(), d.url, UpdatesChannel)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { hub.Close() })
			removed, unsubscribe, err := hub.Subscribe(UpdatesChannel)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(unsubscribe)
			d.pushes.answering(func(endpoint string) int {
				if endpoint == gone.endpoint {
					return code
				}
				return http.StatusCreated
			})

			notifier := d.notifier(t, DefaultTiming())
			reconcile(t, notifier)
			d.one(t, gone)
			d.one(t, kept)
			select {
			case <-removed:
			case <-time.After(awaitBound):
				t.Fatal("the removal was not announced")
			}
			subscriptions, err := d.subscriptions.List(t.Context(), testOperator)
			if err != nil {
				t.Fatal(err)
			}
			if len(subscriptions) != 1 || subscriptions[0].Endpoint != kept.endpoint {
				t.Errorf("subscriptions %+v, want only %s", subscriptions, kept.endpoint)
			}
			reconcile(t, notifier)
			d.none(t, gone)
		})
	}
}

// The push service holds the first pushes until both browsers have one in flight, which two replicas reach only if
// each took a browser and passed over the one the other held. Then both passes finish, and neither sends again.
func TestReplicasSendEachBrowserItsPushOnce(t *testing.T) {
	d := newDelivery(t)
	browsers := []browser{newBrowser(t, "test-first-browser"), newBrowser(t, "test-second-browser")}
	d.subscribe(t, browsers...)
	d.submit(t, "test-pending", false)
	var mu sync.Mutex
	held := 0
	both := make(chan struct{})
	d.pushes.answering(func(string) int {
		mu.Lock()
		held++
		if held == len(browsers) {
			close(both)
		}
		mu.Unlock()
		<-both
		return http.StatusCreated
	})

	replicas := []*Notifier{d.notifier(t, DefaultTiming()), d.notifier(t, DefaultTiming())}
	passes := make(chan error, len(replicas))
	for _, replica := range replicas {
		go func() { passes <- replica.reconcile(t.Context()) }()
	}
	select {
	case <-both:
	case <-time.After(awaitBound):
		t.Fatal("the replicas did not each take a browser")
	}
	for range replicas {
		if err := <-passes; err != nil {
			t.Fatal(err)
		}
	}
	for _, b := range browsers {
		d.one(t, b)
	}
	for _, replica := range replicas {
		reconcile(t, replica)
	}
	for _, b := range browsers {
		d.none(t, b)
	}
}

// run runs the Notifier until the test ends, woken by each commit to a request.
func (d *delivery) run(t *testing.T, notifier *Notifier) {
	t.Helper()
	hub, err := wakeups.Listen(t.Context(), d.url, store.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	changes, unsubscribe, err := hub.Subscribe(store.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	ran := make(chan struct{})
	go func() {
		defer close(ran)
		notifier.Run(ctx, changes)
	}()
	t.Cleanup(func() {
		cancel()
		<-ran
		unsubscribe()
		hub.Close()
	})
}

func TestRunningDeliveryPushesOnACommitWithoutWaitingForTheSweep(t *testing.T) {
	d := newDelivery(t)
	b := newBrowser(t, "test-browser")
	d.subscribe(t, b)
	d.run(t, d.notifier(t, Timing{Sweep: time.Hour, Retry: time.Hour, Send: 10 * time.Second}))
	request := d.submit(t, "test-pending", false)
	if got := b.read(t, d.pushes.await(t, b, 1)[0], d.applicationServerKey); got.Kind != "show" || got.ActionID != request.ID.String() {
		t.Errorf("push %+v, want the request shown", got)
	}
}

func TestRunningDeliveryRetriesARefusedPushOnTheSweep(t *testing.T) {
	d := newDelivery(t)
	b := newBrowser(t, "test-browser")
	d.subscribe(t, b)
	request := d.submit(t, "test-pending", false)
	var refused sync.Once
	d.pushes.answering(func(string) int {
		status := http.StatusCreated
		refused.Do(func() { status = http.StatusServiceUnavailable })
		return status
	})
	// No commit follows the request, so only the sweep sends the refused push again.
	d.run(t, d.notifier(t, Timing{Sweep: 50 * time.Millisecond, Retry: time.Hour, Send: 10 * time.Second}))
	for _, push := range d.pushes.await(t, b, 2)[:2] {
		if got := b.read(t, push, d.applicationServerKey); got.Kind != "show" || got.ActionID != request.ID.String() {
			t.Errorf("push %+v, want the request shown", got)
		}
	}
}
