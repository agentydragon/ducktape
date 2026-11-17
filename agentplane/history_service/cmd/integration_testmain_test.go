package main

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/testing"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(historytesting.RunIntegrationTests(m, &integrationDatabase, useAPIServer))
}
