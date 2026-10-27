package github

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var database *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		migrations.Up,
		func(database *postgres.Database) int {
			database = database
			return m.Run()
		},
	))
}

const (
	nextSHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	// The repository the tests follow and the one a pull request's head can be in.
	baseRepository     = 100
	baseInstallation   = 11
	forkRepository     = 200
	forkInstallation   = 22
	otherInstallation  = 33
	baseRepositoryName = "owner/repo"
)

// world is the GitHub the provider reads: `owner/repo` (100), which the App reaches through installation 11, and
// `fork/repo` (200), through 22. Pull request 7 has the head `head`, in the fork when `fork` is set. Any branch is
// at `head`, and does not exist while it is empty.
type world struct {
	t *testing.T

	mu               sync.Mutex
	head             string
	fork             bool
	revoked, limited bool
	// onRequest runs before a request is answered, outside the lock; it may block until the request is cancelled.
	onRequest func(ctx context.Context, path string)
}

func (w *world) update(change func(*world)) {
	w.mu.Lock()
	defer w.mu.Unlock()
	change(w)
}

func (w *world) serve(rw http.ResponseWriter, r *http.Request, path string) bool {
	w.mu.Lock()
	head, fork, revoked, limited, hook := w.head, w.fork, w.revoked, w.limited, w.onRequest
	w.mu.Unlock()
	if hook != nil {
		hook(r.Context(), path)
	}
	reply := func(status int, body string) {
		rw.WriteHeader(status)
		fmt.Fprint(rw, body)
	}
	last := path[strings.LastIndex(path, "/")+1:]
	inFork := strings.Contains(path, "/fork/repo") || path == "/repositories/200"
	switch {
	case limited:
		rw.Header().Set("X-RateLimit-Remaining", "0")
		rw.Header().Set("Retry-After", "120")
		reply(http.StatusForbidden, "")
	case revoked:
		reply(http.StatusNotFound, "")
	case strings.HasSuffix(path, "/access_tokens"):
		return false
	case strings.HasSuffix(path, "/installation"):
		if inFork {
			reply(http.StatusOK, fmt.Sprintf(`{"id": %d}`, forkInstallation))
		} else {
			reply(http.StatusOK, fmt.Sprintf(`{"id": %d}`, baseInstallation))
		}
	case path == "/repos/owner/repo" || path == "/repositories/100":
		reply(http.StatusOK, fmt.Sprintf(`{"id": %d, "full_name": %q}`, baseRepository, baseRepositoryName))
	case path == "/repos/fork/repo" || path == "/repositories/200":
		reply(http.StatusOK, fmt.Sprintf(`{"id": %d, "full_name": "fork/repo"}`, forkRepository))
	case strings.Contains(path, "/pulls/"):
		repository := fmt.Sprintf(`{"id": %d, "full_name": %q}`, baseRepository, baseRepositoryName)
		if fork {
			repository = fmt.Sprintf(`{"id": %d, "full_name": "fork/repo"}`, forkRepository)
		}
		reply(http.StatusOK, fmt.Sprintf(`{"number": %s, "head": {"sha": %q, "repo": %s}}`, last, head, repository))
	case strings.Contains(path, "/issues/"):
		reply(http.StatusOK, fmt.Sprintf(`{"number": %s}`, last))
	case strings.Contains(path, "/git/ref/"):
		if head == "" {
			reply(http.StatusNotFound, "")
		} else {
			reply(http.StatusOK, fmt.Sprintf(`{"object": {"sha": %q}}`, head))
		}
	case strings.Contains(path, "/commits/"):
		reply(http.StatusOK, fmt.Sprintf(`{"sha": %q}`, last))
	default:
		w.t.Errorf("unexpected request %s %s", r.Method, path)
		reply(http.StatusInternalServerError, "")
	}
	return true
}

var (
	testOwner       = store.ServiceAccount{Namespace: "test", Name: "owner"}
	testDestination = store.DestinationRef{Namespace: "test", Name: "sandbox", UID: "uid"}
	// testIncarnation is testOwner as the Sandbox testDestination names.
	testIncarnation = store.Owner{Account: testOwner, SandboxUID: testDestination.UID}
	// baseAccess is the access to the repository the tests follow.
	baseAccess = store.GitHubAccessKey{AppID: testAppID, InstallationID: baseInstallation, RepositoryID: baseRepository}
	forkAccess = store.GitHubAccessKey{AppID: testAppID, InstallationID: forkInstallation, RepositoryID: forkRepository}
)

// harness is a Provider over real PostgreSQL and the world.
type harness struct {
	t        *testing.T
	db       *sql.DB
	fake     *fakeGitHub
	world    *world
	store    *store.Store
	provider *Provider
}

// newHarness starts from a database without the other tests' rows: the worker claims whichever inbox is due.
func newHarness(t *testing.T) *harness {
	t.Helper()
	db, err := sql.Open("postgres", database.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	h := &harness{t: t, db: db, fake: newFake(t), world: &world{t: t, head: testSHA}}
	h.fake.handler = h.world.serve
	h.exec(`TRUNCATE inbox, github_installation, github_repository, github_delivery CASCADE`)
	h.restart()
	return h
}

// restart is a new process over the same rows: nothing but them carries over.
func (h *harness) restart() {
	h.t.Helper()
	h.store = store.New(h.db, store.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000})
	settings := DefaultSettings()
	settings.AppID = testAppID
	h.provider = NewProvider(newTestClient(h.t, h.fake), h.store, settings)
}

func (h *harness) exec(query string, args ...any) {
	h.t.Helper()
	if _, err := h.db.ExecContext(h.t.Context(), query, args...); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) count(query string, args ...any) int {
	h.t.Helper()
	var n int
	if err := h.db.QueryRowContext(h.t.Context(), query, args...).Scan(&n); err != nil {
		h.t.Fatal(err)
	}
	return n
}

func (h *harness) text(query string, args ...any) string {
	h.t.Helper()
	var value string
	if err := h.db.QueryRowContext(h.t.Context(), query, args...).Scan(&value); err != nil {
		h.t.Fatal(err)
	}
	return value
}

func pullRequestSource(number int64, events ...store.EventFilter) store.GitHubSource {
	source := store.GitHubSource{Repository: baseRepositoryName, Subject: store.PullRequestSubject{Number: number}}
	if len(events) > 0 {
		source.Events = events
	}
	return source
}

func (h *harness) follow(source store.GitHubSource, key string) store.Subscription {
	h.t.Helper()
	access, err := h.provider.Authorize(h.t.Context(), source)
	if err != nil {
		h.t.Fatal(err)
	}
	subscription, err := h.store.Subscribe(h.t.Context(), testOwner, store.Subscribe{
		DestinationRef: testDestination, SessionID: "session", IdempotencyKey: key, Source: source, LifetimeDays: 7,
	}, &access)
	if err != nil {
		h.t.Fatal(err)
	}
	return subscription
}

// deliver is a webhook delivery of the event, as signed HTTP ingress hands it to the store.
func (h *harness) deliver(event string, payload map[string]any) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	ingest, err := ParseDelivery(testAppID, event, uuid.New(), raw)
	if err != nil {
		return err
	}
	_, err = h.store.IngestGitHub(h.t.Context(), ingest)
	return err
}

func (h *harness) ingest(event string, payload map[string]any) {
	h.t.Helper()
	if err := h.deliver(event, payload); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) claim() store.Claim {
	h.t.Helper()
	claim, err := h.store.Claim(h.t.Context())
	if err != nil || claim == nil {
		h.t.Fatalf("Claim = %+v, %v", claim, err)
	}
	return *claim
}

// next is the claimed inbox's next due subscription.
func (h *harness) next(claim store.Claim) store.DueSubscription {
	h.t.Helper()
	due, err := h.store.Source(h.t.Context(), claim)
	if err != nil || due == nil {
		h.t.Fatalf("Source = %+v, %v; want a due subscription", due, err)
	}
	return *due
}

func (h *harness) requireNoneDue(claim store.Claim) {
	h.t.Helper()
	if due, err := h.store.Source(h.t.Context(), claim); err != nil || due != nil {
		h.t.Fatalf("Source = %+v, %v; want none due", due, err)
	}
}

// reconcile reads the claimed inbox's next due subscription, and fails the test if that fails.
func (h *harness) reconcile(claim store.Claim) {
	h.t.Helper()
	if err := h.provider.Reconcile(h.t.Context(), claim, h.next(claim)); err != nil {
		h.t.Fatal(err)
	}
}

// reconcileAll reads subscriptions until none is due.
func (h *harness) reconcileAll(claim store.Claim) {
	h.t.Helper()
	for range 10 {
		due, err := h.store.Source(h.t.Context(), claim)
		if err != nil {
			h.t.Fatal(err)
		}
		if due == nil {
			return
		}
		if err := h.provider.Reconcile(h.t.Context(), claim, *due); err != nil {
			h.t.Fatal(err)
		}
	}
	h.t.Fatal("subscriptions are still due after 10 reads")
}

func (h *harness) page(inboxID uuid.UUID) store.InboxPage {
	h.t.Helper()
	page, err := h.store.Read(h.t.Context(), testIncarnation, inboxID, 0, 128)
	if err != nil {
		h.t.Fatal(err)
	}
	return page
}

func (h *harness) subscription(id uuid.UUID) store.Subscription {
	h.t.Helper()
	subscription, err := h.store.GetSubscription(h.t.Context(), testIncarnation, id)
	if err != nil {
		h.t.Fatal(err)
	}
	return subscription
}

// payloads are the payloads of the entries, as JSON values.
func payloads(t *testing.T, entries []store.Entry) []any {
	t.Helper()
	values := make([]any, len(entries))
	for i, entry := range entries {
		if err := json.Unmarshal(entry.Payload, &values[i]); err != nil {
			t.Fatal(err)
		}
	}
	return values
}

// asJSON is the payload as the value an entry's payload decodes to.
func asJSON(t *testing.T, payload map[string]any) any {
	t.Helper()
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		t.Fatal(err)
	}
	return value
}

func requireEqual(t *testing.T, got, want any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

// sortedIDs are ids in the order entries list them.
func sortedIDs(ids ...uuid.UUID) []uuid.UUID {
	slices.SortFunc(ids, func(a, b uuid.UUID) int { return strings.Compare(a.String(), b.String()) })
	return ids
}

func repositoryPayload(id int64, name string) map[string]any {
	return map[string]any{"id": id, "full_name": name}
}

// comment is a comment on pull request number, delivered to the base repository.
func comment(number int64) map[string]any {
	return map[string]any{
		"action": "created", "installation": map[string]any{"id": baseInstallation},
		"repository": repositoryPayload(baseRepository, baseRepositoryName),
		"issue":      map[string]any{"number": number, "pull_request": map[string]any{"url": "https://api.github.test/pulls/7"}},
		"comment":    map[string]any{"body": "kept verbatim"},
	}
}

// pullRequestPayload is a push to pull request 7, whose head repository is not named.
func pullRequestPayload(sha string) map[string]any {
	return map[string]any{
		"action": "synchronize", "installation": map[string]any{"id": baseInstallation},
		"repository":   repositoryPayload(baseRepository, baseRepositoryName),
		"pull_request": map[string]any{"number": 7, "head": map[string]any{"sha": sha}},
	}
}

// check is a completed check run on the commit, delivered to the base repository.
func check(sha string) map[string]any {
	return map[string]any{
		"action": "completed", "installation": map[string]any{"id": baseInstallation},
		"repository": repositoryPayload(baseRepository, baseRepositoryName),
		"check_run":  map[string]any{"head_sha": sha, "pull_requests": []any{}},
	}
}

// checkInFork is a completed check run in the fork, delivered under an installation.
func checkInFork(sha string, installation int64) map[string]any {
	payload := check(sha)
	payload["repository"] = repositoryPayload(forkRepository, "fork/repo")
	payload["installation"] = map[string]any{"id": installation}
	return payload
}

func installationEvent(action string) map[string]any {
	return map[string]any{"installation": map[string]any{"id": baseInstallation}, "action": action}
}
