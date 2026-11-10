// Package migrations owns Goose migrations for the MCP edge's `mcp_edge` database.
package migrations

import (
	"context"
	"database/sql"
	"embed"

	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
)

//go:embed mcp_edge/*.sql
var scripts embed.FS

var schema = migrate.Schema{Files: scripts, Dir: "mcp_edge", Table: "mcp_edge_goose_version", LockID: 0x4D43_5045}

// Up applies the service-owned migrations before runtime startup.
func Up(ctx context.Context, db *sql.DB) error { return schema.Up(ctx, db) }
