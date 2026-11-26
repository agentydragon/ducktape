package main

import (
	"fmt"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	workloadidentitytesting "github.com/agentydragon/ducktape/agentplane/identity/credentials/testing"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	// The sandbox group's boxes request WorkloadCredentials, whose issuer writes Certificates.
	certificates, err := workloadidentitytesting.CertificateCRD()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(postgres.RunIntegrationTests(func() int {
		return kubeapiserver.Run(m, useAPIServer, certificates)
	}, migrations.Up, &integrationDatabase))
}
