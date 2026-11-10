// Package migrations owns Goose migrations for the Notification Service's notifications database.
package migrations

import (
	"context"
	"database/sql"
	"embed"

	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
)

//go:embed notifications/*.sql
var scripts embed.FS

var schema = migrate.Schema{
	Files: scripts, Dir: "notifications", Table: "notification_service_goose_version",
	// Retain the advisory lock key shared with the previous Goose and Alembic migration paths.
	LockID: 0x4E4F_5446,
}

// Up applies schema changes before the service starts. Goose serializes concurrent service starts with a session lock.
func Up(ctx context.Context, db *sql.DB) error { return schema.Up(ctx, db) }
