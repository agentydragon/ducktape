package executor

import (
	"context"
	"database/sql"
	"os"
	"testing"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	migrate := func(ctx context.Context, db *sql.DB) error { return migrations.Up(ctx, db) }
	os.Exit(postgres.RunIntegrationTests(migrate, func(database *postgres.Database) int {
		integrationDatabase = database
		return m.Run()
	}))
}

func newTestStore(t testing.TB) *sessionstore.Store {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return sessionstore.New(db, 30*time.Second)
}

func openStoreSession(t *testing.T, store *sessionstore.Store, request *samplerpb.CreateSessionRequest, ownerUID string, api protocol.APIStyle) (*sessionstore.Session, error) {
	t.Helper()
	if request.GetExecutionIdentity() == nil {
		request.ExecutionIdentity = &identitypb.Identity{Name: "fixture-session-identity", Uid: "fixture-session-identity-uid"}
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
	platformInstructions := "Fixture Agentplane platform guidance."
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
