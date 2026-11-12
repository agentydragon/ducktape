package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"
	"log/slog"
	"os"
	"sync"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	migrate := func(ctx context.Context, db *sql.DB) error { return Migrate(ctx, db) }
	os.Exit(postgres.RunIntegrationTests(migrate, func(database *postgres.Database) int {
		integrationDatabase = database
		return m.Run()
	}))
}

func newTestStoreWithDSN(t testing.TB) (*Store, string) {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return New(db), dsn
}

func newTestStore(t testing.TB) *Store {
	t.Helper()
	store, _ := newTestStoreWithDSN(t)
	return store
}

// capturedLog is one structured record: its message, level and attribute values.
type capturedLog map[string]any

type logCapture struct {
	mu      sync.Mutex
	records []capturedLog
}

func (c *logCapture) Enabled(context.Context, slog.Level) bool { return true }

func (c *logCapture) Handle(_ context.Context, record slog.Record) error {
	fields := capturedLog{"msg": record.Message, "level": record.Level.String()}
	record.Attrs(func(attr slog.Attr) bool {
		fields[attr.Key] = attr.Value.Any()
		return true
	})
	c.mu.Lock()
	defer c.mu.Unlock()
	c.records = append(c.records, fields)
	return nil
}

func (c *logCapture) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *logCapture) WithGroup(string) slog.Handler      { return c }

// captureLogs routes the default logger into the test until cleanup; the returned function
// finds the first record with the given message and fails the test when none was logged.
func captureLogs(t testing.TB) func(message string) capturedLog {
	t.Helper()
	capture := &logCapture{}
	previous := slog.Default()
	slog.SetDefault(slog.New(capture))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return func(message string) capturedLog {
		t.Helper()
		capture.mu.Lock()
		defer capture.mu.Unlock()
		for _, record := range capture.records {
			if record["msg"] == message {
				return record
			}
		}
		t.Fatalf("no log record %q among %d records", message, len(capture.records))
		return nil
	}
}

func nativeResponsesRecords(t testing.TB, items ...responses.Item) []json.RawMessage {
	t.Helper()
	records := make([]json.RawMessage, len(items))
	for index, item := range items {
		encoded, err := json.Marshal(item)
		if err != nil {
			t.Fatalf("marshal native Responses record %d: %v", index+1, err)
		}
		records[index] = encoded
	}
	return records
}

func responsesItems(t testing.TB, state protocol.Context) []responses.Item {
	t.Helper()
	if state == nil || state.API() != protocol.APIStyleResponses {
		t.Fatalf("native context is %T, want Responses", state)
	}
	records, err := state.Records()
	if err != nil {
		t.Fatal(err)
	}
	items := make([]responses.Item, 0, len(records))
	for _, raw := range records {
		var item responses.Item
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatal(err)
		}
		items = append(items, item)
	}
	return items
}
