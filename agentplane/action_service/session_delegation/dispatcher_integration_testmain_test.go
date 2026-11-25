package sessiondelegation

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var dispatcherDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(m.Run, migrations.Up, &dispatcherDatabase))
}
