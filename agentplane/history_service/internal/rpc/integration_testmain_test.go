package rpc

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/testing"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		historytesting.SetupIntegrationDatabase,
		func(database *postgres.Database) int {
			integrationDatabase = database
			return kubeapiserver.Run(m, useAPIServer)
		},
	))
}
