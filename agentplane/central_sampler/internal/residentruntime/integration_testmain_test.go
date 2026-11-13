package residentruntime

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"log/slog"
	"os"
	"reflect"
	"sync"
	"testing"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	if runCodeModePeer() {
		return
	}
	migrate := func(ctx context.Context, db *sql.DB) error { return migrations.Up(ctx, db) }
	os.Exit(postgres.RunIntegrationTests(migrate, func(database *postgres.Database) int {
		integrationDatabase = database
		return m.Run()
	}))
}

func newTestStoreWithDSN(t testing.TB) (*sessionstore.Store, string) {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return sessionstore.New(db, 30*time.Second), dsn
}

func newTestStore(t testing.TB) *sessionstore.Store {
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

const fixturePlatformInstructions = "Fixture Agentplane platform guidance."

func fixtureExecutionIdentity() *identitypb.Identity {
	return &identitypb.Identity{Name: "fixture-session-identity", Uid: "fixture-session-identity-uid"}
}

func openStoreSession(t *testing.T, store *sessionstore.Store, request *samplerpb.CreateSessionRequest, ownerUID string, api protocol.APIStyle) (*sessionstore.Session, error) {
	t.Helper()
	if request.GetExecutionIdentity() == nil {
		request.ExecutionIdentity = fixtureExecutionIdentity()
	}
	if request.GetExecutor() == nil {
		request.Executor = &identitypb.SandboxDestination{
			Owner:  &identitypb.ServiceAccount{Namespace: "fixture", Name: "executor"},
			Target: &identitypb.EnvironmentTarget{Namespace: "fixture", Name: "fixture-sandbox", Uid: "fixture-sandbox-uid"},
		}
	}
	if request.GetSpec() != nil && request.GetSpec().GetPlacement() == nil {
		request.Spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{
			ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: request.GetExecutor().GetTarget().GetName(), Uid: request.GetExecutor().GetTarget().GetUid()},
		}}}
	}
	platformInstructions := fixturePlatformInstructions
	return store.CreateSession(t.Context(), request, ownerUID, api, &platformInstructions)
}

func openSessionForStore(t *testing.T, store *sessionstore.Store, setup string) *sessionstore.Session {
	t.Helper()
	request := &samplerpb.CreateSessionRequest{
		SessionId:   uuid.NewString(),
		Spec:        &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
		SetupScript: &setup,
	}
	session, err := openStoreSession(t, store, request, "fixture-account", protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	return session
}

func claimedExecutorStore(t *testing.T) (*sessionstore.Store, sessionstore.Owner) {
	t.Helper()
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	return store, owner
}

func assertNoUnresolvedNestedIntents(t *testing.T, store *sessionstore.Store, owner sessionstore.Owner) {
	t.Helper()
	var count int
	if err := store.DB().QueryRowContext(t.Context(), "SELECT count(*) FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL", owner.ConversationID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("unresolved nested intents=%d", count)
	}
}

func admitInput(t *testing.T, store *sessionstore.Store, sessionID, commandID, text string) {
	t.Helper()
	if _, err := store.Admit(t.Context(), sessionID, &commandpb.Command{CommandId: commandID, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: text}}}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
}

func admitProviderCommand(t *testing.T, store *sessionstore.Store, owner sessionstore.Owner, sessionID, commandID string, compact bool) {
	t.Helper()
	if compact {
		if _, err := store.Admit(t.Context(), sessionID, &commandpb.Command{CommandId: commandID, Operation: &commandpb.Command_Compact{Compact: &commandpb.Compact{}}}, "fixture-account", nil); err != nil {
			t.Fatal(err)
		}
	} else {
		admitInput(t, store, sessionID, commandID, "provider retry fixture")
	}
	command, err := store.NextCommand(t.Context(), owner, sessionID)
	if err != nil || command == nil || command.ID != commandID {
		t.Fatalf("next provider command=%v err=%v", command, err)
	}
	if _, err := store.PrepareSamplingContext(t.Context(), owner, &sessionstore.ExecutionState{}); err != nil {
		t.Fatal(err)
	}
}

func providerAttemptTestPolicy() sessionstore.ProviderAttemptPolicy {
	return sessionstore.ProviderAttemptPolicy{Budget: 3, Lease: time.Minute}
}

func providerAttemptTestPolicyPointer() *sessionstore.ProviderAttemptPolicy {
	policy := providerAttemptTestPolicy()
	return &policy
}

func equivalentJSON(t *testing.T, got, want []byte) {
	t.Helper()
	decode := func(raw []byte) any {
		t.Helper()
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err != nil {
			t.Fatalf("decode JSON %q: %v", raw, err)
		}
		return value
	}
	if left, right := decode(got), decode(want); !reflect.DeepEqual(left, right) {
		gotJSON, _ := json.Marshal(left)
		wantJSON, _ := json.Marshal(right)
		t.Fatalf("JSON differs:\n got: %s\nwant: %s", gotJSON, wantJSON)
	}
}
