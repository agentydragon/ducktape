package main

import (
	"fmt"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	workloadidentitytesting "github.com/agentydragon/ducktape/agentplane/workload_identity/testing"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	// The sandbox group's boxes request WorkloadCredentials, whose issuer writes Certificates.
	certificates, err := workloadidentitytesting.CertificateCRD()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(postgres.RunIntegrationTests(
		migrations.Up,
		func(database *postgres.Database) int {
			integrationDatabase = database
			return kubeapiserver.Run(m, useAPIServer, certificates)
		},
	))
}
