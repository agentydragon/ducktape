package webhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/github/settings"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/google/uuid"
)

const (
	testSecret = "test-webhook-secret-0123456789"
	testAppID  = 4242
	// bodyLimit is the smallest max_body_bytes the settings allow.
	bodyLimit = 1024
	// awaitBound only fails a test whose wakeup never comes.
	awaitBound = 30 * time.Second
)

func testSettings() settings.Settings {
	settings := settings.DefaultSettings()
	settings.AppID = testAppID
	settings.PrivateKey = "unused-private-key"
	settings.WebhookSecret = testSecret
	settings.MaxBodyBytes = bodyLimit
	return settings
}

func openDatabase(t *testing.T, url string) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func newHandler(db *sql.DB, configure func(*settings.Settings)) *Handler {
	settings := testSettings()
	if configure != nil {
		configure(&settings)
	}
	return New(settings, store.New(db, store.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000}))
}

// serve is the URL of the route served over HTTP, as the facade serves it.
func serve(t *testing.T, handler http.Handler) string {
	t.Helper()
	mux := http.NewServeMux()
	mux.Handle(Pattern, handler)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server.URL + "/v1/webhooks/github"
}

// route is a Handler over the shared database, served over HTTP.
type route struct {
	*Handler
	URL string
	db  *sql.DB
}

func newRoute(t *testing.T, configure func(*settings.Settings)) route {
	t.Helper()
	db := openDatabase(t, integrationDatabase.URL)
	handler := newHandler(db, configure)
	return route{Handler: handler, URL: serve(t, handler), db: db}
}

// delivery is a request to the route. Its fields are the header texts, so that a test can corrupt each.
type delivery struct {
	event     string
	id        string
	body      []byte
	signature string
}

func sign(body []byte) string {
	mac := hmac.New(sha256.New, []byte(testSecret))
	mac.Write(body)
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

func signedDelivery(event string, body []byte) delivery {
	return delivery{event: event, id: uuid.NewString(), body: body, signature: sign(body)}
}

func (d delivery) request(t *testing.T, url string, body io.Reader) *http.Request {
	t.Helper()
	request, err := http.NewRequestWithContext(t.Context(), http.MethodPost, url, body)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("X-GitHub-Event", d.event)
	request.Header.Set("X-GitHub-Delivery", d.id)
	request.Header.Set("X-Hub-Signature-256", d.signature)
	request.Header.Set("Content-Type", "application/json")
	return request
}

type answer struct {
	status int
	header http.Header
	body   []byte
}

func (a answer) decode(t *testing.T, into any) {
	t.Helper()
	if err := json.Unmarshal(a.body, into); err != nil {
		t.Fatalf("answer %d is not the expected JSON: %v: %s", a.status, err, a.body)
	}
}

func (a answer) expect(t *testing.T, status int, want any) {
	t.Helper()
	if a.status != status {
		t.Fatalf("status %d, want %d: %s", a.status, status, a.body)
	}
	got := reflect.New(reflect.TypeOf(want))
	a.decode(t, got.Interface())
	if !reflect.DeepEqual(got.Elem().Interface(), want) {
		t.Fatalf("answer %s, want %+v", a.body, want)
	}
}

func read(t *testing.T, response *http.Response) answer {
	t.Helper()
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return answer{status: response.StatusCode, header: response.Header, body: body}
}

func (d delivery) post(t *testing.T, url string) answer {
	t.Helper()
	response, err := http.DefaultClient.Do(d.request(t, url, bytes.NewReader(d.body)))
	if err != nil {
		t.Fatal(err)
	}
	return read(t, response)
}

// stored is how many deliveries of this ID the database holds.
func (r route) stored(t *testing.T, id string) int {
	t.Helper()
	var count int
	if err := r.db.QueryRowContext(t.Context(),
		`SELECT count(*) FROM github_delivery WHERE app_id = $1 AND delivery_id = $2`, testAppID, id).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func (r route) storedPayload(t *testing.T, id string) []byte {
	t.Helper()
	var payload []byte
	if err := r.db.QueryRowContext(t.Context(),
		`SELECT payload FROM github_delivery WHERE app_id = $1 AND delivery_id = $2`, testAppID, id).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	return payload
}

func sameJSON(t *testing.T, got, want []byte) {
	t.Helper()
	var gotValue, wantValue any
	if err := json.Unmarshal(got, &gotValue); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(want, &wantValue); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(gotValue, wantValue) {
		t.Fatalf("stored payload %s, want %s", got, want)
	}
}

func issueComment(number int, comment string) []byte {
	return fmt.Appendf(nil,
		`{"action":"created","installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},"issue":{"number":%d},"comment":{"body":%q}}`,
		number, comment)
}

// issueCommentOfSize is an issue_comment body of exactly size bytes.
func issueCommentOfSize(t *testing.T, size int) []byte {
	t.Helper()
	empty := issueComment(7, "")
	if len(empty) > size {
		t.Fatalf("an issue_comment body is at least %d bytes", len(empty))
	}
	return issueComment(7, strings.Repeat("x", size-len(empty)))
}

// logBuffer collects log lines; the handler logs from the server's goroutines.
type logBuffer struct {
	mu   sync.Mutex
	data bytes.Buffer
}

func (b *logBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.data.Write(p)
}

func (b *logBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.data.String()
}

// rejections are the records of refused deliveries.
func (b *logBuffer) rejections(t *testing.T) []map[string]any {
	t.Helper()
	var records []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(b.String()), "\n") {
		if line == "" {
			continue
		}
		var record map[string]any
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			t.Fatalf("log line is not JSON: %v: %s", err, line)
		}
		if record["msg"] == "GitHub webhook rejected" {
			records = append(records, record)
		}
	}
	return records
}

func captureLogs(t *testing.T) *logBuffer {
	t.Helper()
	logs := &logBuffer{}
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(logs, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return logs
}

func TestAcceptedDeliveryIsStoredOnceAndRedeliveredAsDuplicate(t *testing.T) {
	r := newRoute(t, nil)
	d := signedDelivery("issue_comment", issueComment(7, "hello"))

	first := d.post(t, r.URL)
	first.expect(t, http.StatusAccepted, receipt{Accepted: true})
	if contentType := first.header.Get("Content-Type"); contentType != "application/json" {
		t.Errorf("Content-Type %q", contentType)
	}
	sameJSON(t, r.storedPayload(t, d.id), d.body)

	d.post(t, r.URL).expect(t, http.StatusAccepted, receipt{Accepted: true, Duplicate: true})
	if count := r.stored(t, d.id); count != 1 {
		t.Errorf("%d deliveries stored", count)
	}
}

func TestInstallationEventWithoutRepositoryIsAccepted(t *testing.T) {
	r := newRoute(t, nil)
	d := signedDelivery("installation", []byte(`{"action":"created","installation":{"id":12}}`))

	d.post(t, r.URL).expect(t, http.StatusAccepted, receipt{Accepted: true})
	if count := r.stored(t, d.id); count != 1 {
		t.Errorf("%d deliveries stored", count)
	}
}

func TestRedeliveryWithOtherContentIsAConflict(t *testing.T) {
	r := newRoute(t, nil)
	first := signedDelivery("issue_comment", issueComment(7, "first"))
	first.post(t, r.URL).expect(t, http.StatusAccepted, receipt{Accepted: true})

	changed := signedDelivery("issue_comment", issueComment(7, "changed"))
	changed.id = first.id
	changed.post(t, r.URL).expect(t, http.StatusConflict, refusal{Detail: store.ErrDeliveryReused.Error()})
	sameJSON(t, r.storedPayload(t, first.id), first.body)
}

func TestPingIsAcceptedUnparsedAndStoresNothing(t *testing.T) {
	r := newRoute(t, nil)
	// GitHub's ping carries a JSON body, but the route accepts what is signed without reading it.
	d := signedDelivery("ping", []byte("not JSON"))

	d.post(t, r.URL).expect(t, http.StatusAccepted, receipt{Accepted: true})
	if count := r.stored(t, d.id); count != 0 {
		t.Errorf("%d deliveries stored", count)
	}

	d.signature = "sha256=" + strings.Repeat("0", 64)
	d.post(t, r.URL).expect(t, http.StatusUnauthorized, refusal{Detail: "invalid GitHub signature"})
}

// The example of GitHub's "Validating webhook deliveries" documentation, which the other tests' own signing cannot
// stand in for.
func TestSignatureIsTheHMACGitHubDocuments(t *testing.T) {
	settings := testSettings()
	settings.WebhookSecret = "It's a Secret to Everybody"
	handler := New(settings, nil)

	if !handler.signed([]byte("Hello, World!"), "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17") {
		t.Error("the documented signature was refused")
	}
}

func TestSignatureMustBeTheBodysExactHMAC(t *testing.T) {
	r := newRoute(t, nil)
	valid := sign(issueComment(7, "signed"))
	for name, signature := range map[string]string{
		"missing":             "",
		"zeros":               "sha256=" + strings.Repeat("0", 64),
		"no algorithm prefix": strings.TrimPrefix(valid, "sha256="),
		"upper case":          "sha256=" + strings.ToUpper(strings.TrimPrefix(valid, "sha256=")),
		"another body's":      sign(issueComment(7, "another")),
		"not ASCII":           "sha256=é",
	} {
		t.Run(name, func(t *testing.T) {
			d := signedDelivery("issue_comment", issueComment(7, "signed"))
			d.signature = signature
			d.post(t, r.URL).expect(t, http.StatusUnauthorized, refusal{Detail: "invalid GitHub signature"})
			if count := r.stored(t, d.id); count != 0 {
				t.Errorf("%d deliveries stored", count)
			}
		})
	}
}

func TestRefusedDeliveriesAreLoggedBoundedAndRedacted(t *testing.T) {
	const sentinel = "private-payload-sentinel"
	longEvent := strings.Repeat("x", 200)
	r := newRoute(t, nil)
	noRepository := []byte(`{"action":"created","installation":{"id":11},"issue":{"number":7}}`)
	badInstallation := []byte(fmt.Sprintf(`{"action":"created","installation":{"id":%q},"repository":{"id":100,"full_name":"owner/repo"}}`, sentinel))
	notJSON := []byte("{invalid " + sentinel)

	for _, c := range []struct {
		name    string
		event   string
		body    []byte
		corrupt func(*delivery)
		status  int
		detail  string
		reason  string
		hasID   bool
	}{
		{
			name: "delivery ID not a UUID", event: "issue_comment", body: issueComment(7, sentinel),
			corrupt: func(d *delivery) { d.id = sentinel + "-id" },
			status:  http.StatusBadRequest, detail: invalidDelivery, reason: "invalid_delivery_id",
		},
		{
			name: "delivery ID is checked before the signature", event: "issue_comment", body: issueComment(7, sentinel),
			corrupt: func(d *delivery) { d.id, d.signature = sentinel+"-id", "sha256=bad-signature-sentinel" },
			status:  http.StatusBadRequest, detail: invalidDelivery, reason: "invalid_delivery_id",
		},
		{
			name: "bad signature", event: "issue_comment", body: issueComment(7, sentinel),
			corrupt: func(d *delivery) { d.signature = "sha256=bad-signature-sentinel" },
			status:  http.StatusUnauthorized, detail: "invalid GitHub signature", reason: "invalid_signature", hasID: true,
		},
		{
			name: "unsupported event", event: "unknown_event", body: issueComment(7, sentinel),
			status: http.StatusBadRequest, detail: invalidDelivery, reason: "unsupported_event", hasID: true,
		},
		{
			name: "no event", event: "", body: issueComment(7, sentinel),
			status: http.StatusBadRequest, detail: invalidDelivery, reason: "unsupported_event", hasID: true,
		},
		{
			name: "event longer than the log bound", event: longEvent, body: issueComment(7, sentinel),
			status: http.StatusBadRequest, detail: invalidDelivery, reason: "unsupported_event", hasID: true,
		},
		{
			name: "the header, not the body, selects the event", event: "check_suite", body: issueComment(7, sentinel),
			status: http.StatusBadRequest, detail: invalidDelivery, reason: "payload_validation", hasID: true,
		},
		{
			name: "event about a repository without one", event: "issue_comment", body: noRepository,
			status: http.StatusBadRequest, detail: invalidDelivery, reason: "missing_repository", hasID: true,
		},
		{
			name: "field of the wrong type", event: "issue_comment", body: badInstallation,
			status: http.StatusBadRequest, detail: invalidDelivery, reason: "payload_validation", hasID: true,
		},
		{
			name: "not JSON", event: "issue_comment", body: notJSON,
			status: http.StatusBadRequest, detail: invalidDelivery, reason: "payload_validation", hasID: true,
		},
	} {
		t.Run(c.name, func(t *testing.T) {
			logs := captureLogs(t)
			d := signedDelivery(c.event, c.body)
			if c.corrupt != nil {
				c.corrupt(&d)
			}
			signed := d.signature
			d.post(t, r.URL).expect(t, c.status, refusal{Detail: c.detail})

			records := logs.rejections(t)
			if len(records) != 1 {
				t.Fatalf("%d rejection records, want 1: %s", len(records), logs)
			}
			record := records[0]
			if record["reason"] != c.reason {
				t.Errorf("reason %v, want %s", record["reason"], c.reason)
			}
			wantEvent := c.event
			if len(wantEvent) > maxLoggedEvent {
				wantEvent = wantEvent[:maxLoggedEvent]
			}
			if record["event"] != wantEvent || record["event_truncated"] != (len(c.event) > maxLoggedEvent) {
				t.Errorf("event %v (truncated %v), want %q", record["event"], record["event_truncated"], wantEvent)
			}
			if _, has := record["delivery_id"]; has != c.hasID {
				t.Errorf("delivery_id logged = %v, want %v", has, c.hasID)
			}
			if c.hasID && record["delivery_id"] != d.id {
				t.Errorf("delivery_id %v, want %s", record["delivery_id"], d.id)
			}
			for _, secret := range []string{sentinel, testSecret, signed, string(c.body)} {
				if secret != "" && strings.Contains(logs.String(), secret) {
					t.Errorf("log carries %q: %s", secret, logs)
				}
			}
			if c.hasID {
				if count := r.stored(t, d.id); count != 0 {
					t.Errorf("%d deliveries stored", count)
				}
			}
		})
	}
}

func TestBodyLimitIsInclusiveAndCheckedBeforeTheSignature(t *testing.T) {
	r := newRoute(t, nil)

	atLimit := signedDelivery("issue_comment", issueCommentOfSize(t, bodyLimit))
	atLimit.post(t, r.URL).expect(t, http.StatusAccepted, receipt{Accepted: true})

	over := signedDelivery("issue_comment", issueCommentOfSize(t, bodyLimit+1))
	over.signature = sign([]byte("a body that was not sent"))
	over.post(t, r.URL).expect(t, http.StatusRequestEntityTooLarge, refusal{Detail: "GitHub payload exceeds configured limit"})
	if count := r.stored(t, over.id); count != 0 {
		t.Errorf("%d deliveries stored", count)
	}
}

// signalingBody reports the first read of a request body, which the handler makes once it holds a slot. Every request
// of a test shares the once, so only the first read signals.
type signalingBody struct {
	io.ReadCloser
	once    *sync.Once
	started chan struct{}
}

func (b *signalingBody) Read(p []byte) (int, error) {
	b.once.Do(func() { close(b.started) })
	return b.ReadCloser.Read(p)
}

type outcome struct {
	answer answer
	err    error
}

// postStreamed sends the delivery with a body the test finishes by hand, and delivers the answer on the channel.
func (d delivery) postStreamed(t *testing.T, url string) (*io.PipeWriter, <-chan outcome) {
	t.Helper()
	reader, writer := io.Pipe()
	t.Cleanup(func() { writer.Close() })
	answers := make(chan outcome, 1)
	request := d.request(t, url, reader)
	go func() {
		response, err := http.DefaultClient.Do(request)
		if err != nil {
			answers <- outcome{err: err}
			return
		}
		defer response.Body.Close()
		body, err := io.ReadAll(response.Body)
		answers <- outcome{answer: answer{status: response.StatusCode, header: response.Header, body: body}, err: err}
	}()
	return writer, answers
}

func TestSaturatedReplicaRefusesInsteadOfQueueingAndFreesTheSlotAfterwards(t *testing.T) {
	db := openDatabase(t, integrationDatabase.URL)
	handler := newHandler(db, func(s *settings.Settings) { s.WebhookConcurrency = 1 })
	started := make(chan struct{})
	var once sync.Once
	url := serve(t, http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		request.Body = &signalingBody{ReadCloser: request.Body, once: &once, started: started}
		handler.ServeHTTP(w, request)
	}))
	r := route{Handler: handler, URL: url, db: db}

	holder := signedDelivery("issue_comment", issueComment(7, "holds the slot"))
	body, holderAnswer := holder.postStreamed(t, url)
	<-started

	// The holder is inside the handler, reading its body, so the one slot is taken.
	waiting := signedDelivery("issue_comment", issueComment(8, "refused"))
	busy := waiting.post(t, url)
	busy.expect(t, http.StatusServiceUnavailable, refusal{Detail: "GitHub ingress busy"})
	if retry := busy.header.Get("Retry-After"); retry != "5" {
		t.Errorf("Retry-After %q", retry)
	}
	if count := r.stored(t, waiting.id); count != 0 {
		t.Errorf("%d refused deliveries stored", count)
	}

	if _, err := body.Write(holder.body); err != nil {
		t.Fatal(err)
	}
	body.Close()
	finished := <-holderAnswer
	if finished.err != nil {
		t.Fatal(finished.err)
	}
	finished.answer.expect(t, http.StatusAccepted, receipt{Accepted: true})

	waiting.post(t, url).expect(t, http.StatusAccepted, receipt{Accepted: true})
}

func TestBodyThatNeverFinishesIsCutOffAtTheBound(t *testing.T) {
	r := newRoute(t, nil)
	r.bound = 300 * time.Millisecond
	d := signedDelivery("issue_comment", issueComment(7, "slow"))

	body, answers := d.postStreamed(t, r.URL)
	go func() { _, _ = body.Write(d.body[:10]) }()
	result := <-answers
	if result.err != nil {
		t.Fatal(result.err)
	}
	result.answer.expect(t, http.StatusServiceUnavailable, refusal{Detail: backendDown})
	if count := r.stored(t, d.id); count != 0 {
		t.Errorf("%d deliveries stored", count)
	}
}

func TestCommitThatStallsIsCutOffAtTheBoundAndStoresNothing(t *testing.T) {
	r := newRoute(t, nil)
	r.bound = 2 * time.Second
	d := signedDelivery("issue_comment", issueComment(7, "stalled"))

	// A lock on the table the ingest reads first stalls it behind the transaction that holds it.
	blocker, err := r.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { blocker.Rollback() })
	if _, err := blocker.ExecContext(t.Context(), `LOCK TABLE github_delivery IN ACCESS EXCLUSIVE MODE`); err != nil {
		t.Fatal(err)
	}

	d.post(t, r.URL).expect(t, http.StatusServiceUnavailable, refusal{Detail: backendDown})
	if err := blocker.Rollback(); err != nil {
		t.Fatal(err)
	}
	if count := r.stored(t, d.id); count != 0 {
		t.Errorf("%d deliveries stored by the cut-off attempt", count)
	}
	d.post(t, r.URL).expect(t, http.StatusAccepted, receipt{Accepted: true})
}

func TestUnreachableDatabaseIsUnavailable(t *testing.T) {
	// Nothing listens on port 1.
	db := openDatabase(t, "postgres://postgres:postgres@127.0.0.1:1/postgres?sslmode=disable")
	url := serve(t, newHandler(db, nil))

	signedDelivery("issue_comment", issueComment(7, "nowhere")).post(t, url).
		expect(t, http.StatusServiceUnavailable, refusal{Detail: backendDown})
}

func TestNewDeliveryWakesWorkersAndRedeliveryDoesNot(t *testing.T) {
	const sentinel = "webhook_test_sentinel"
	r := newRoute(t, nil)
	hub, err := wakeups.Listen(t.Context(), integrationDatabase.URL, store.WorkChannel, sentinel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	work, stopWork, err := hub.Subscribe(store.WorkChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(stopWork)
	settled, stopSettled, err := hub.Subscribe(sentinel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(stopSettled)

	// The hub applies notifications in the order PostgreSQL delivers them, so once the sentinel's has arrived, every
	// notification the delivery committed before it has too.
	wakes := func(d delivery, want receipt) bool {
		t.Helper()
		d.post(t, r.URL).expect(t, http.StatusAccepted, want)
		ctx, cancel := context.WithTimeout(t.Context(), awaitBound)
		defer cancel()
		if _, err := r.db.ExecContext(ctx, `SELECT pg_notify($1, '')`, sentinel); err != nil {
			t.Fatal(err)
		}
		select {
		case <-settled:
		case <-ctx.Done():
			t.Fatal("no wakeup from the sentinel")
		}
		select {
		case <-work:
			return true
		default:
			return false
		}
	}
	d := signedDelivery("issue_comment", issueComment(7, "wakes"))
	if !wakes(d, receipt{Accepted: true}) {
		t.Error("a new delivery did not wake the workers")
	}
	if wakes(d, receipt{Accepted: true, Duplicate: true}) {
		t.Error("a redelivery woke the workers")
	}
}
