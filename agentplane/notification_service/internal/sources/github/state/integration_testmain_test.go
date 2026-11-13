package state

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(migrations.Up, func(database *postgres.Database) int {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		if err := database.PrepareCloneTemplate(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "prepare pristine PostgreSQL clone template:", err)
			return 1
		}
		integrationDatabase = database
		return m.Run()
	}))
}
