package linkage_test

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		migrations.Up,
		func(database *postgres.Database) int {
			integrationDatabase = database
			return m.Run()
		},
	))
}

// awaitBound only fails a test whose condition never comes.
const awaitBound = 30 * time.Second

var operator = store.OperatorPrincipal{Issuer: "https://test-issuer.example", Subject: "test-operator"}

// fixture is a database of the test's own.
type fixture struct {
	db *sql.DB
	// changes is signalled after each commit that notifies linkage.UpdatesChannel.
	changes <-chan struct{}
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	hub, err := wakeups.Listen(t.Context(), dsn, linkage.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	changes, unsubscribe, err := hub.Subscribe(linkage.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(unsubscribe)
	return fixture{db: db, changes: changes}
}

// authority is a replica's Authority of the servers, which use no shared Client ID Metadata Document.
func (f fixture) authority(t *testing.T, servers ...linkage.Server) *linkage.Authority {
	t.Helper()
	return f.authorityWith(t, linkage.ClientMetadata{}, servers...)
}

func (f fixture) authorityWith(t *testing.T, metadata linkage.ClientMetadata, servers ...linkage.Server) *linkage.Authority {
	t.Helper()
	configured := map[string]linkage.Server{}
	for _, server := range servers {
		configured[server.ServerID] = server
	}
	if err := linkage.Validate(configured, metadata); err != nil {
		t.Fatal(err)
	}
	authority, err := linkage.New(f.db, configured, metadata)
	if err != nil {
		t.Fatal(err)
	}
	return authority
}

// changed waits for a commit that notified linkage.UpdatesChannel since the last it waited for.
func (f fixture) changed(t *testing.T, what string) {
	t.Helper()
	select {
	case <-f.changes:
	case <-time.After(awaitBound):
		t.Fatalf("%s notified nothing", what)
	}
}

// tokenRevision is the revision of the server's stored token.
func (f fixture) tokenRevision(t *testing.T, serverID string) int {
	t.Helper()
	var revision int
	if err := f.db.QueryRowContext(t.Context(), "SELECT token_revision FROM mcp_oauth_token_state WHERE server_id = $1",
		serverID).Scan(&revision); err != nil {
		t.Fatal(err)
	}
	return revision
}

// linkageOf is the linkage of the server serverID, as the authority lists it.
func linkageOf(t *testing.T, authority *linkage.Authority, serverID string) *operator_action_servicepb.McpServerLinkage {
	t.Helper()
	linkages, _, err := authority.Linkages(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	for _, found := range linkages {
		if found.GetServerId() == serverID {
			return found
		}
	}
	t.Fatalf("no linkage of %s", serverID)
	return nil
}
