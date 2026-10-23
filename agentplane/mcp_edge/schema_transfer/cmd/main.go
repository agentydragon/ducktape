// mcp-edge-transfer is an operator-run, one-shot copy from Actions to the MCP edge database.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/mcp_edge/schema_transfer"
)

const (
	actionsDatabaseURLEnv = "AGENTPLANE_ACTIONS_DATABASE_URL"
	edgeDatabaseURLEnv    = "AGENTPLANE_MCP_EDGE_DATABASE_URL"
)

func main() {
	confirmed := flag.Bool("confirm-single-writer-cutover", false,
		"confirm Actions is stopped, the edge has no destination writers, and Actions will stay stopped until its old Connection and OAuth handlers and writers are removed")
	flag.Parse()
	if !*confirmed {
		fmt.Fprintln(os.Stderr, "refusing transfer: pass --confirm-single-writer-cutover only after confirming its full precondition")
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	result, err := schema_transfer.Run(ctx, os.Getenv(actionsDatabaseURLEnv), os.Getenv(edgeDatabaseURLEnv))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for _, tableName := range []string{
		"external_connection", "external_connection_grant", "connection_enrollment",
		"oauth_registered_client", "oauth_authorization", "oauth_token",
	} {
		fmt.Printf("copied %s: %d rows\n", tableName, result.Rows[tableName])
	}
}
