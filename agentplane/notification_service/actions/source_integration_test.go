package actions

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"github.com/google/uuid"
)

const (
	testNamespace = "test-namespace"
	testSandbox   = "test-sandbox"
)

// testOwner owns the inboxes: a Sandbox runs as the ServiceAccount of its own name.
var testOwner = store.ServiceAccount{Namespace: testNamespace, Name: testSandbox}

const testOwnerJSON = `{"namespace": "test-namespace", "name": "test-sandbox"}`

// wireEvent is event sequence as the Action Service's events endpoint sends it.
func wireEvent(sequence int64) string {
	return fmt.Sprintf(`{"sequence": %d, "state": "decision_pending", "at": "2026-10-20T03:56:03.123456Z", "actor": null}`, sequence)
}

type recordedRequest struct {
	Path          string
	Query         url.Values
	Authorization string
}

// fakeActionService serves the two reads of the Action Service for one Action request, whose log has events
// with sequences 1 through its count, unless a test takes a request over.
type fakeActionService struct {
	mu       sync.Mutex
	caller   string
	events   int64
	takeover func(w http.ResponseWriter, r *http.Request) bool
	requests []recordedRequest
}

func (s *fakeActionService) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.requests = append(s.requests, recordedRequest{r.URL.Path, r.URL.Query(), r.Header.Get("Authorization")})
	caller, events, takeover := s.caller, s.events, s.takeover
	s.mu.Unlock()
	if takeover != nil && takeover(w, r) {
		return
	}
	if strings.HasSuffix(r.URL.Path, "/events") {
		after, _ := strconv.ParseInt(r.URL.Query().Get("after_sequence"), 10, 64)
		limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
		var page []string
		for sequence := after + 1; sequence <= events && len(page) < limit; sequence++ {
			page = append(page, wireEvent(sequence))
		}
		fmt.Fprintf(w, "[%s]", strings.Join(page, ","))
		return
	}
	fmt.Fprintf(w, `{"id": %q, "title": "Test Action", "state": "decision_pending", "caller": %s}`,
		strings.TrimPrefix(r.URL.Path, "/v1/action-requests/"), caller)
}

func (s *fakeActionService) set(f func(*fakeActionService)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f(s)
}

func (s *fakeActionService) log() []recordedRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]recordedRequest(nil), s.requests...)
}

func (s *fakeActionService) paths() []string {
	var paths []string
	for _, request := range s.log() {
		paths = append(paths, request.Path)
	}
	return paths
}

type fixture struct {
	db        *sql.DB
	store     *store.Store
	service   *fakeActionService
	server    *httptest.Server
	tokenFile string
	source    *Source
}

// newFixture starts from a database without the other tests' inboxes: Claim takes whichever inbox is due.
func newFixture(t *testing.T) fixture {
	t.Helper()
	db, err := sql.Open("postgres", database.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.ExecContext(t.Context(), `TRUNCATE inbox CASCADE`); err != nil {
		t.Fatal(err)
	}
	service := &fakeActionService{caller: testOwnerJSON}
	server := httptest.NewServer(service)
	t.Cleanup(server.Close)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	f := fixture{
		db: db, service: service, server: server, tokenFile: tokenFile,
		store: store.New(db, store.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000}),
	}
	f.source = &Source{Service: f.client(t, 5), Store: f.store}
	return f
}

func (f fixture) client(t *testing.T, timeoutSeconds float64) *Client {
	t.Helper()
	client, err := NewClient(Settings{URL: f.server.URL, TokenFile: f.tokenFile, RequestTimeoutSeconds: timeoutSeconds})
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func (f fixture) subscribe(t *testing.T) store.Subscription {
	t.Helper()
	subscription, err := f.store.Subscribe(t.Context(), testOwner, store.Subscribe{
		DestinationRef: store.DestinationRef{Namespace: testNamespace, Name: testSandbox, UID: "test-uid"},
		SessionID:      "test-session",
		IdempotencyKey: "test-key",
		Source:         store.ActionsSource{RequestID: uuid.New()},
		LifetimeDays:   7,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	return subscription
}

func (f fixture) claim(t *testing.T) store.Claim {
	t.Helper()
	claim, err := f.store.Claim(t.Context())
	if err != nil || claim == nil {
		t.Fatalf("claim = %v, %v", claim, err)
	}
	return *claim
}

// reconcile reads the subscription's source as the worker does when it is due.
func (f fixture) reconcile(t *testing.T, claim store.Claim) error {
	t.Helper()
	return f.reconcileWith(t, f.source, claim)
}

func (f fixture) reconcileWith(t *testing.T, source *Source, claim store.Claim) error {
	t.Helper()
	if _, err := f.db.ExecContext(t.Context(), `UPDATE subscription SET next_attempt = now() - interval '1 second'`); err != nil {
		t.Fatal(err)
	}
	due, err := f.store.Source(t.Context(), claim)
	if err != nil || due == nil {
		t.Fatalf("source = %v, %v", due, err)
	}
	return source.Reconcile(t.Context(), claim, *due)
}

func (f fixture) entries(t *testing.T, inboxID uuid.UUID) []store.Entry {
	t.Helper()
	page, err := f.store.Read(t.Context(), testOwner, inboxID, 0, 128)
	if err != nil {
		t.Fatal(err)
	}
	return page.Entries
}

// lastCursor is the cursor of the inbox's newest entry, which is how many entries it holds.
func (f fixture) lastCursor(t *testing.T, inboxID uuid.UUID) int64 {
	t.Helper()
	page, err := f.store.Read(t.Context(), testOwner, inboxID, 0, 1)
	if err != nil {
		t.Fatal(err)
	}
	return page.Inbox.LastCursor
}

func requireFailure(t *testing.T, err error, kind store.SourceFailureKind, message string) {
	t.Helper()
	var failure *worker.ProviderError
	if !errors.As(err, &failure) {
		t.Fatalf("error = %v, want a *worker.ProviderError", err)
	}
	if failure.Kind != kind || failure.Message != message || failure.RetryAfter != 0 {
		t.Fatalf("failure = %+v, want kind %s, message %q and the default retry", *failure, kind, message)
	}
}

func TestReconcileRecordsTheEventsOfTheCallersRequest(t *testing.T) {
	f := newFixture(t)
	subscription := f.subscribe(t)
	request := subscription.Source.(store.ActionsSource).RequestID
	f.service.set(func(s *fakeActionService) { s.events = 3 })
	claim := f.claim(t)

	if err := f.reconcile(t, claim); err != nil {
		t.Fatal(err)
	}

	entries := f.entries(t, subscription.InboxID)
	if len(entries) != 3 {
		t.Fatalf("entries = %+v, want 3", entries)
	}
	for i, entry := range entries {
		sequence := int64(i + 1)
		if !reflect.DeepEqual(entry.Event, store.ActionsEvent{RequestID: request, Sequence: sequence}) {
			t.Errorf("entry %d is %+v", sequence, entry.Event)
		}
		if got, want := string(entry.Payload), wireEvent(sequence); !sameJSON(t, got, want) {
			t.Errorf("payload %s, want %s", got, want)
		}
	}
	requests := f.service.log()
	if len(requests) != 2 {
		t.Fatalf("requests = %+v, want the request and its events", requests)
	}
	if requests[0].Path != "/v1/action-requests/"+request.String() || requests[1].Path != requests[0].Path+"/events" {
		t.Errorf("paths = %q and %q", requests[0].Path, requests[1].Path)
	}
	if got := requests[1].Query; got.Get("after_sequence") != "0" || got.Get("limit") != "128" {
		t.Errorf("events query = %v", got)
	}

	// The token is read again for every request, so its rotation is followed; reading again resumes after the
	// last sequence recorded.
	if err := os.WriteFile(f.tokenFile, []byte("rotated"), 0o600); err != nil {
		t.Fatal(err)
	}
	f.service.set(func(s *fakeActionService) { s.events = 5 })
	if err := f.reconcile(t, claim); err != nil {
		t.Fatal(err)
	}
	requests = f.service.log()[2:]
	if len(requests) != 2 || requests[1].Query.Get("after_sequence") != "3" {
		t.Errorf("second read = %+v", requests)
	}
	if first := f.service.log()[0].Authorization; first != "Bearer test-token" {
		t.Errorf("Authorization = %q", first)
	}
	for _, request := range requests {
		if request.Authorization != "Bearer rotated" {
			t.Errorf("Authorization after rotation = %q", request.Authorization)
		}
	}
	if got := len(f.entries(t, subscription.InboxID)); got != 5 {
		t.Errorf("entries = %d, want 5", got)
	}
}

func TestReconcileReadsALongLogAPageAtATime(t *testing.T) {
	f := newFixture(t)
	subscription := f.subscribe(t)
	f.service.set(func(s *fakeActionService) { s.events = 130 })
	claim := f.claim(t)

	if err := f.reconcile(t, claim); err != nil {
		t.Fatal(err)
	}
	if got := f.lastCursor(t, subscription.InboxID); got != 128 {
		t.Fatalf("entries after the first read = %d, want 128", got)
	}
	if err := f.reconcile(t, claim); err != nil {
		t.Fatal(err)
	}
	if got := f.lastCursor(t, subscription.InboxID); got != 130 {
		t.Errorf("entries after the second read = %d, want 130", got)
	}
}

func TestReconcileDoesNotFollowARequestOfAnotherAccount(t *testing.T) {
	for name, caller := range map[string]string{
		"another account":                    `{"namespace": "test-namespace", "name": "another-sandbox"}`,
		"the same name in another namespace": `{"namespace": "another-namespace", "name": "test-sandbox"}`,
		"a caller the service hides":         `null`,
	} {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			subscription := f.subscribe(t)
			f.service.set(func(s *fakeActionService) { s.caller, s.events = caller, 3 })

			err := f.reconcile(t, f.claim(t))

			requireFailure(t, err, store.FailureAccessDenied, "SourceNotOwnedError")
			// The service's broad read access is not authority to follow what it reads: the events are not read.
			for _, path := range f.service.paths() {
				if strings.HasSuffix(path, "/events") {
					t.Errorf("events were requested: %q", path)
				}
			}
			if got := len(f.entries(t, subscription.InboxID)); got != 0 {
				t.Errorf("entries = %d", got)
			}
		})
	}
}

func TestEventsReturnsTheOwnersEventsAndRefusesAnotherAccount(t *testing.T) {
	f := newFixture(t)
	f.service.set(func(s *fakeActionService) { s.events = 2 })
	request := uuid.New()

	events, err := f.source.Events(t.Context(), testOwner, request, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 || events[0].Sequence != 2 || events[0].State != store.ActionDecisionPending {
		t.Errorf("events = %+v, want the second", events)
	}

	_, err = f.source.Events(t.Context(), store.ServiceAccount{Namespace: testNamespace, Name: "another-sandbox"}, request, 0)
	requireFailure(t, err, store.FailureAccessDenied, "SourceNotOwnedError")
}

func TestReconcileClassifiesWhatTheActionServiceAnswers(t *testing.T) {
	const (
		request = "the request"
		events  = "its events"
	)
	for name, c := range map[string]struct {
		read    string
		status  int
		body    string
		kind    store.SourceFailureKind
		message string
	}{
		"rate limited":            {request, 429, "", store.FailureRateLimited, "HTTP 429"},
		"unauthenticated":         {request, 401, "", store.FailureAccessDenied, "HTTP 401"},
		"forbidden":               {request, 403, "", store.FailureAccessDenied, "HTTP 403"},
		"request not found":       {request, 404, "", store.FailureAccessDenied, "HTTP 404"},
		"server error":            {request, 500, "", store.FailureUnavailable, "HTTP 500"},
		"bad gateway":             {request, 502, "", store.FailureUnavailable, "HTTP 502"},
		"unavailable":             {request, 503, "", store.FailureUnavailable, "HTTP 503"},
		"bad request":             {request, 400, "", store.FailureProcessingError, "HTTP 400"},
		"conflict":                {request, 409, "", store.FailureProcessingError, "HTTP 409"},
		"unprocessable":           {request, 422, "", store.FailureProcessingError, "HTTP 422"},
		"request that is no JSON": {request, 200, "not json", store.FailureProcessingError, "ValidationError"},
		"request that is a list":  {request, 200, "[]", store.FailureProcessingError, "ValidationError"},
		"request without caller":  {request, 200, `{"id": "x"}`, store.FailureProcessingError, "ValidationError"},
		"caller without a name":   {request, 200, `{"caller": {"namespace": "test-namespace"}}`, store.FailureProcessingError, "ValidationError"},
		"events rate limited":     {events, 429, "", store.FailureRateLimited, "HTTP 429"},
		"events forbidden":        {events, 403, "", store.FailureAccessDenied, "HTTP 403"},
		"events not found":        {events, 404, "", store.FailureAccessDenied, "HTTP 404"},
		"events server error":     {events, 500, "", store.FailureUnavailable, "HTTP 500"},
		"events unprocessable":    {events, 422, "", store.FailureProcessingError, "HTTP 422"},
		"events that are no JSON": {events, 200, "not json", store.FailureProcessingError, "ValidationError"},
		"events that are an object": {
			events, 200, `{}`, store.FailureProcessingError, "ValidationError",
		},
		"event with a field the model lacks": {
			events, 200, `[{"sequence": 1, "state": "allowed", "at": "2026-10-20T03:56:03Z", "extra": 1}]`,
			store.FailureProcessingError, "ValidationError",
		},
		"event in a state the model lacks": {
			events, 200, `[{"sequence": 1, "state": "paused", "at": "2026-10-20T03:56:03Z"}]`,
			store.FailureProcessingError, "ValidationError",
		},
		"redirected request": {request, 302, "", store.FailureProcessingError, "HTTP 302"},
		"redirected events":  {events, 307, "", store.FailureProcessingError, "HTTP 307"},
	} {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t)
			subscription := f.subscribe(t)
			f.service.set(func(s *fakeActionService) {
				s.events = 3
				s.takeover = func(w http.ResponseWriter, r *http.Request) bool {
					if strings.HasSuffix(r.URL.Path, "/events") != (c.read == events) {
						return false
					}
					if c.status >= 300 && c.status < 400 {
						w.Header().Set("Location", "/elsewhere")
					}
					w.WriteHeader(c.status)
					fmt.Fprint(w, c.body)
					return true
				}
			})

			err := f.reconcile(t, f.claim(t))

			requireFailure(t, err, c.kind, c.message)
			for _, path := range f.service.paths() {
				if path == "/elsewhere" {
					t.Error("a redirect was followed")
				}
			}
			if got := len(f.entries(t, subscription.InboxID)); got != 0 {
				t.Errorf("entries = %d", got)
			}
		})
	}
}

func TestReconcileShowsAnUnreachableActionServiceAsUnavailable(t *testing.T) {
	t.Run("connection refused", func(t *testing.T) {
		f := newFixture(t)
		f.subscribe(t)
		claim := f.claim(t)
		f.server.Close()

		requireFailure(t, f.reconcile(t, claim), store.FailureUnavailable, "TransportError")
	})
	t.Run("no answer within the timeout", func(t *testing.T) {
		f := newFixture(t)
		f.subscribe(t)
		claim := f.claim(t)
		f.service.set(func(s *fakeActionService) {
			s.takeover = func(_ http.ResponseWriter, r *http.Request) bool {
				<-r.Context().Done()
				return true
			}
		})
		impatient := &Source{Service: f.client(t, 0.05), Store: f.store}

		requireFailure(t, f.reconcileWith(t, impatient, claim), store.FailureUnavailable, "TransportError")
	})
}

// A cancelled read is the worker stopping, not a source to show as failing.
func TestReconcileReturnsTheCancellationOfItsContext(t *testing.T) {
	f := newFixture(t)
	f.subscribe(t)
	claim := f.claim(t)
	if _, err := f.db.ExecContext(t.Context(), `UPDATE subscription SET next_attempt = now() - interval '1 second'`); err != nil {
		t.Fatal(err)
	}
	due, err := f.store.Source(t.Context(), claim)
	if err != nil || due == nil {
		t.Fatalf("source = %v, %v", due, err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()

	err = f.source.Reconcile(ctx, claim, *due)

	var failure *worker.ProviderError
	if !errors.Is(err, context.Canceled) || errors.As(err, &failure) {
		t.Errorf("error = %v, want the context's cancellation", err)
	}
}

func TestReconcileRefusesAGapInTheLogAndRecordsNothing(t *testing.T) {
	f := newFixture(t)
	subscription := f.subscribe(t)
	f.service.set(func(s *fakeActionService) {
		s.takeover = func(w http.ResponseWriter, r *http.Request) bool {
			if !strings.HasSuffix(r.URL.Path, "/events") {
				return false
			}
			fmt.Fprintf(w, "[%s,%s]", wireEvent(2), wireEvent(3))
			return true
		}
	})

	err := f.reconcile(t, f.claim(t))

	requireFailure(t, err, store.FailureProcessingError, "ConflictError")
	if got := len(f.entries(t, subscription.InboxID)); got != 0 {
		t.Errorf("entries = %d", got)
	}
}

// Python stops the worker when the token file cannot be read; the failure is not one to show and retry.
func TestAnUnreadableTokenIsNotAFailureOfTheSubscription(t *testing.T) {
	f := newFixture(t)
	f.subscribe(t)
	claim := f.claim(t)
	if err := os.Remove(f.tokenFile); err != nil {
		t.Fatal(err)
	}

	err := f.reconcile(t, claim)

	var failure *worker.ProviderError
	if err == nil || errors.As(err, &failure) {
		t.Errorf("error = %v, want one that is not a *worker.ProviderError", err)
	}
	if got := f.service.paths(); len(got) != 0 {
		t.Errorf("requests were made without a token: %q", got)
	}
}

func sameJSON(t *testing.T, got, want string) bool {
	t.Helper()
	var a, b any
	if err := json.Unmarshal([]byte(got), &a); err != nil {
		t.Fatalf("%s: %v", got, err)
	}
	if err := json.Unmarshal([]byte(want), &b); err != nil {
		t.Fatalf("%s: %v", want, err)
	}
	return reflect.DeepEqual(a, b)
}
