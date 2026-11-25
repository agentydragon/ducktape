package session

import (
	"os"
	"testing"
	"time"

	runtimetesting "github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	if runtimetesting.RunCodeModePeer() {
		return
	}
	os.Exit(postgres.RunIntegrationTests(m.Run, migrations.Up, &integrationDatabase))
}

func claimedExecutorStore(t *testing.T) (*sessionstore.Store, sessionstore.Owner) {
	t.Helper()
	store, _, owner := runtimetesting.ClaimedSession(t, integrationDatabase)
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
	if _, err := store.PrepareSamplingContext(t.Context(), owner); err != nil {
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
