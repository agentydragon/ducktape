package session

import (
	"bytes"
	"encoding/json"
	"os"
	"reflect"
	"testing"
	"time"

	runtimetesting "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/residentruntime/testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	if runtimetesting.RunCodeModePeer() {
		return
	}
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, migrations.Up, &integrationDatabase))
}

func newTestStoreWithDSN(t testing.TB) (*sessionstore.Store, string) {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db := postgres.Open(t, dsn)
	return sessionstore.New(db, 30*time.Second), dsn
}

func newTestStore(t testing.TB) *sessionstore.Store {
	t.Helper()
	store, _ := newTestStoreWithDSN(t)
	return store
}

func claimedExecutorStore(t *testing.T) (*sessionstore.Store, sessionstore.Owner) {
	t.Helper()
	store := newTestStore(t)
	session := runtimetesting.OpenSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	return store, owner
}

func admitProviderCommand(t *testing.T, store *sessionstore.Store, owner sessionstore.Owner, sessionID, commandID string, compact bool) {
	t.Helper()
	if compact {
		if _, err := store.Admit(t.Context(), sessionID, &commandpb.Command{CommandId: commandID, Operation: &commandpb.Command_Compact{Compact: &commandpb.Compact{}}}, "fixture-account", nil); err != nil {
			t.Fatal(err)
		}
	} else {
		runtimetesting.AdmitInput(t, store, sessionID, commandID, "provider retry fixture")
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
