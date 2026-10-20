package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"sync"
	"testing"
)

const testCredential = "agentplane-credential-test-placeholder"

type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// cli is one invocation of the command with a fixed environment.
type cli struct {
	stdout, stderr syncBuffer
	app            *app
}

func newCLI(env map[string]string, stdin string) *cli {
	c := &cli{}
	c.app = newApp(func(name string) string { return env[name] }, strings.NewReader(stdin), &c.stdout, &c.stderr)
	return c
}

// run executes the command line and returns the exit status.
func (c *cli) run(args ...string) int {
	return c.app.execute(args)
}

// invoke runs one command line and returns its exit status, stdout and stderr.
func invoke(env map[string]string, args ...string) (int, string, string) {
	c := newCLI(env, "")
	code := c.run(args...)
	return code, c.stdout.String(), c.stderr.String()
}

func actionsEnv(baseURL string) map[string]string {
	return map[string]string{"AGENTPLANE_ACTIONS_URL": baseURL, "AGENTPLANE_ACTIONS_CREDENTIAL": testCredential}
}

func notificationsEnv(baseURL string) map[string]string {
	return map[string]string{
		"AGENTPLANE_NOTIFICATIONS_URL":        baseURL,
		"AGENTPLANE_NOTIFICATIONS_CREDENTIAL": testCredential,
		"AGENTPLANE_DESTINATION":              testDestination,
	}
}

func egressEnv(baseURL string) map[string]string {
	return map[string]string{"AGENTPLANE_EGRESS_URL": baseURL, "AGENTPLANE_EGRESS_CREDENTIAL": testCredential}
}

const testDestination = `{"destination_ref": {"namespace": "test-ns", "name": "test-sandbox", "uid": "test-uid"}, "session_id": "test-session"}`

// seen is a request a fake service received.
type seen struct {
	method, path  string
	query         url.Values
	body          []byte
	authorization string
}

// fake is a service that records what it was asked and answers with handler.
type fake struct {
	*httptest.Server
	mu   sync.Mutex
	seen []seen
}

func newFake(t *testing.T, handler func(w http.ResponseWriter, r *http.Request, s seen)) *fake {
	t.Helper()
	f := &fake{}
	f.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("reading request body: %v", err)
		}
		s := seen{r.Method, r.URL.Path, r.URL.Query(), body, r.Header.Get("Authorization")}
		f.mu.Lock()
		f.seen = append(f.seen, s)
		f.mu.Unlock()
		handler(w, r, s)
	}))
	t.Cleanup(f.Close)
	return f
}

func (f *fake) all() []seen {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.seen)
}

// requests are the recorded requests with this method.
func (f *fake) requests(method string) []seen {
	var matching []seen
	for _, s := range f.all() {
		if s.method == method {
			matching = append(matching, s)
		}
	}
	return matching
}

func (f *fake) count() int {
	return len(f.all())
}

func reply(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = io.WriteString(w, body)
}

// drop ends the connection without answering: the request arrived and the response is lost.
func drop(w http.ResponseWriter) {
	connection, _, err := w.(http.Hijacker).Hijack()
	if err == nil {
		_ = connection.Close()
	}
}

// receiptJSON is an ActionRequest as the Action Service sends it.
func receiptJSON(id, state, key string) string {
	return receiptWithExecution(id, state, key, "null")
}

func receiptWithExecution(id, state, key, execution string) string {
	return fmt.Sprintf(`{"id":%q,"idempotency_key":%q,"action":{"group":"test-group","name":"test-action"},`+
		`"arguments":{},"title":"test title","description":null,"caller":null,"state":%q,"version":1,`+
		`"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","decision":null,"execution":%s}`,
		id, key, state, execution)
}

func succeededWith(result string) string {
	return fmt.Sprintf(`{"id":"00000000-0000-0000-0000-0000000000e1","state":"succeeded","result":%s,"error":null,`+
		`"created_at":"2026-01-01T00:00:00Z","started_at":null,"completed_at":null,"reconciled_at":null}`, result)
}

func decodeObject(t *testing.T, text string) map[string]any {
	t.Helper()
	var object map[string]any
	if err := json.Unmarshal([]byte(text), &object); err != nil {
		t.Fatalf("not a JSON object: %q: %v", text, err)
	}
	return object
}

// errorOf is the error envelope on the last line of stderr.
func errorOf(t *testing.T, stderr string) apiError {
	t.Helper()
	lines := strings.Split(strings.TrimSpace(stderr), "\n")
	var parsed envelope
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &parsed); err != nil {
		t.Fatalf("stderr does not end in an error envelope: %q: %v", stderr, err)
	}
	return parsed.Error
}

func jsonLines(t *testing.T, stdout string) []map[string]any {
	t.Helper()
	var objects []map[string]any
	for line := range strings.SplitSeq(strings.TrimSpace(stdout), "\n") {
		if line != "" {
			objects = append(objects, decodeObject(t, line))
		}
	}
	return objects
}
