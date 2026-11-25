// Package historytesting contains integration test helpers for the history service.
package historytesting

import (
	"context"
	"database/sql"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/internal/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

func SetupIntegrationDatabase(ctx context.Context, db *sql.DB) error {
	if err := postgres.EnsureElectricRole(ctx, db); err != nil {
		return err
	}
	return migrations.Up(ctx, db)
}

// RunIntegrationTests runs the package with its History database and kube-apiserver fixtures.
func RunIntegrationTests(m *testing.M, destination **postgres.Database, setUp func(*kubeapiserver.Server) error) int {
	return postgres.RunIntegrationTests(func() int { return kubeapiserver.Run(m, setUp) }, SetupIntegrationDatabase, destination)
}
