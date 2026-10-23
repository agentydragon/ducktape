// mcp-edge-migrate is an init container, not part of the MCP edge's startup.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"

	"github.com/agentydragon/ducktape/agentplane/mcp_edge/migrations"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
)

const databaseURLEnv = "AGENTPLANE_MCP_EDGE_DATABASE_URL"

func main() {
	databaseURL := os.Getenv(databaseURLEnv)
	if databaseURL == "" {
		fmt.Fprintln(os.Stderr, databaseURLEnv+" is required")
		os.Exit(2)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	if err := migrate.Run(context.Background(), databaseURL, migrations.Up); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
