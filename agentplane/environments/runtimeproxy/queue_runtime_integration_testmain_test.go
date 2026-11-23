package runtimeproxy

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(func() int {
		return kubeapiserver.Run(m, useAPIServer)
	}, migrations.Up, &integrationDatabase))
}
