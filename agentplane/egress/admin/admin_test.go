package admin

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/google/uuid"
)

var started = time.Date(2026, 9, 3, 12, 0, 0, 0, time.UTC)

type fakeDecisions struct {
	records []decision.Record
	err     error
	asked   []*decision.Subject
}

func (f *fakeDecisions) Recent(_ context.Context, subject *decision.Subject) ([]decision.Record, error) {
	f.asked = append(f.asked, subject)
	return f.records, f.err
}

type fakeChanges func(ctx context.Context, changed func() error) error

func (f fakeChanges) Follow(ctx context.Context, changed func() error) error { return f(ctx, changed) }

type fakeHistory history.Health

func (f fakeHistory) Health() history.Health { return history.Health(f) }

type fakeIndex policy.Status

func (f fakeIndex) Status(time.Time) policy.Status { return policy.Status(f) }

func healthyIndex() fakeIndex {
	return fakeIndex{Available: true, Synced: true, StaleAfter: 15 * time.Minute, Ages: map[policy.Kind]time.Duration{policy.Secrets: 0}}
}

func staleIndex() fakeIndex {
	return fakeIndex{Synced: true, StaleAfter: 15 * time.Minute, Ages: map[policy.Kind]time.Duration{policy.Secrets: 20 * time.Minute}}
}

func drainingIndex() fakeIndex {
	return fakeIndex{Synced: true, Draining: true, StaleAfter: time.Minute}
}

func serve(decisions Decisions, changes Changes, health HistoryHealth, index Index) *httptest.Server {
	return httptest.NewServer(New(decisions, changes, health, index, func() time.Time { return started }))
}

func get(t *testing.T, server *httptest.Server, path string) (int, []byte) {
	t.Helper()
	response, err := http.Get(server.URL + path)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, body
}

func record(subject *decision.Subject) decision.Record {
	return decision.Record{
		EventID: uuid.New(), At: started, ProducerID: uuid.New(), Subject: subject, ConnectionID: "test-connection",
		Phase: decision.HTTPRequest, Method: "GET", Host: "example.test", Port: 443, Outcome: decision.Allow,
	}
}

func TestDecisionsSelectOneSubjectOrTheUnauthenticated(t *testing.T) {
	store := &fakeDecisions{records: []decision.Record{record(&decision.Subject{Namespace: "test-namespace", Name: "test-workload"})}}
	server := serve(store, nil, fakeHistory{}, healthyIndex())
	defer server.Close()

	for _, path := range []string{"/decisions?namespace=test-namespace&name=test-workload", "/decisions"} {
		if status, _ := get(t, server, path); status != http.StatusOK {
			t.Fatalf("%s: %d", path, status)
		}
	}
	if len(store.asked) != 2 || *store.asked[0] != (decision.Subject{Namespace: "test-namespace", Name: "test-workload"}) || store.asked[1] != nil {
		t.Fatalf("asked for %v", store.asked)
	}
	for _, path := range []string{"/decisions?namespace=test-namespace", "/decisions?name=test-workload"} {
		if status, _ := get(t, server, path); status != http.StatusBadRequest {
			t.Errorf("%s: %d, want a subject refused unless whole", path, status)
		}
	}
}

// The integration app reads these names.
func TestDecisionsWireNames(t *testing.T) {
	store := &fakeDecisions{records: []decision.Record{record(nil)}}
	server := serve(store, nil, fakeHistory{}, healthyIndex())
	defer server.Close()

	_, body := get(t, server, "/decisions")
	var records []map[string]any
	if err := json.Unmarshal(body, &records); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"at", "method", "host", "port", "outcome", "reason", "binding", "policy", "rule", "substituted", "address"} {
		if _, ok := records[0][name]; !ok {
			t.Errorf("%q missing from %s", name, body)
		}
	}
}

func TestNoDecisionsIsAnEmptyList(t *testing.T) {
	server := serve(&fakeDecisions{records: []decision.Record{}}, nil, fakeHistory{}, healthyIndex())
	defer server.Close()

	if _, body := get(t, server, "/decisions"); strings.TrimSpace(string(body)) != "[]" {
		t.Fatalf("body %s", body)
	}
}

func TestDecisionsReadFailureIsExplicitAndPrivate(t *testing.T) {
	store := &fakeDecisions{err: errors.New("test private exception must not escape")}
	server := serve(store, nil, fakeHistory{}, healthyIndex())
	defer server.Close()

	status, body := get(t, server, "/decisions")
	if status != http.StatusServiceUnavailable || strings.Contains(string(body), "test private exception") {
		t.Fatalf("%d %s", status, body)
	}
}

func TestChangesStreamsFramesAsTheyHappen(t *testing.T) {
	release := make(chan struct{})
	changes := fakeChanges(func(ctx context.Context, changed func() error) error {
		if err := changed(); err != nil {
			return err
		}
		select {
		case <-release:
		case <-ctx.Done():
			return ctx.Err()
		}
		return changed()
	})
	server := serve(nil, changes, fakeHistory{}, healthyIndex())
	defer server.Close()

	response, err := http.Get(server.URL + "/decisions/changes")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.Header.Get("Content-Type") != "text/event-stream" {
		t.Fatalf("content type %q", response.Header.Get("Content-Type"))
	}
	frames := bufio.NewReader(response.Body)
	frame := func() string {
		var lines []string
		for {
			line, err := frames.ReadString('\n')
			if err != nil {
				t.Fatalf("stream broke after %q: %v", lines, err)
			}
			if line == "\n" {
				return strings.Join(lines, "")
			}
			lines = append(lines, line)
		}
	}
	// The first frame arrives while the stream is still open: it was flushed, not buffered to the end.
	if got := frame(); got != "event: changed\ndata: {}\n" {
		t.Fatalf("first frame %q", got)
	}
	close(release)
	if got := frame(); got != "event: changed\ndata: {}\n" {
		t.Fatalf("second frame %q", got)
	}
}

func TestChangesRefusesWhenTheHistoryCannotBeFollowed(t *testing.T) {
	changes := fakeChanges(func(context.Context, func() error) error { return errors.New("test private exception") })
	server := serve(nil, changes, fakeHistory{}, healthyIndex())
	defer server.Close()

	status, body := get(t, server, "/decisions/changes")
	if status != http.StatusServiceUnavailable || strings.Contains(string(body), "test private exception") {
		t.Fatalf("%d %s", status, body)
	}
}

func TestHealthzIsTheIndexVerdictWithItsFacts(t *testing.T) {
	overflowed := fakeHistory{DroppedOverflow: 1}
	for _, test := range []struct {
		name   string
		index  fakeIndex
		status int
	}{
		{"healthy", healthyIndex(), http.StatusOK},
		{"stale", staleIndex(), http.StatusServiceUnavailable},
		{"draining", drainingIndex(), http.StatusServiceUnavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := serve(nil, nil, overflowed, test.index)
			defer server.Close()
			status, raw := get(t, server, "/healthz")
			var body struct {
				Synced              bool
				Draining            bool
				DecisionHistory     map[string]any
				StaleAfterSeconds   float64
				RefreshedSecondsAgo map[string]float64
			}
			if err := json.Unmarshal(raw, &body); err != nil {
				t.Fatal(err)
			}
			if status != test.status || body.Synced != test.index.Synced || body.Draining != test.index.Draining ||
				body.StaleAfterSeconds != test.index.StaleAfter.Seconds() || body.DecisionHistory["dropped_overflow"] != 1.0 ||
				len(body.RefreshedSecondsAgo) != len(test.index.Ages) {
				t.Fatalf("%d %s", status, raw)
			}
		})
	}
}

func TestHealthzRoundsAgesToTenthsOfASecond(t *testing.T) {
	index := healthyIndex()
	index.Ages = map[policy.Kind]time.Duration{policy.Secrets: 1234 * time.Millisecond}
	server := serve(nil, nil, fakeHistory{}, index)
	defer server.Close()

	_, raw := get(t, server, "/healthz")
	var body struct{ RefreshedSecondsAgo map[string]float64 }
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatal(err)
	}
	if body.RefreshedSecondsAgo["secrets"] != 1.2 {
		t.Fatalf("%s", raw)
	}
}

func TestLivenessSurvivesAnUnavailableIndex(t *testing.T) {
	server := serve(nil, nil, fakeHistory{}, fakeIndex{Draining: true})
	defer server.Close()

	if status, _ := get(t, server, "/livez"); status != http.StatusOK {
		t.Fatalf("livez %d", status)
	}
	if status, _ := get(t, server, "/healthz"); status != http.StatusServiceUnavailable {
		t.Fatalf("healthz %d", status)
	}
}
