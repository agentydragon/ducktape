package residentruntime

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"reflect"
	"testing"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
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

const fixturePlatformInstructions = "Fixture Agentplane platform guidance."

func openStoreSession(t *testing.T, store *sessionstore.Store, request *samplerpb.CreateSessionRequest, ownerUID string, api protocol.APIStyle) (*sessionstore.Session, error) {
	t.Helper()
	testsupport.FillCreateSessionRequestDefaults(request)
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
