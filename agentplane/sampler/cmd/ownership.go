package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/ownershiprewrite"
	_ "github.com/lib/pq"
)

const ownershipDatabaseURLEnv = "AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL"

// runOwnershipCommand handles the central sampler image's one-off ownership subcommands. The
// database URL comes from the same Secret-backed environment variable as normal sampler startup.
func runOwnershipCommand(ctx context.Context, args []string, stdout io.Writer) (bool, error) {
	if len(args) == 0 || args[0] != "ownership" {
		return false, nil
	}
	if len(args) < 2 {
		return true, errors.New("ownership requires inventory, forward, or reverse")
	}
	mode := args[1]
	if mode != "inventory" && mode != "forward" && mode != "reverse" {
		return true, fmt.Errorf("unknown ownership mode %q", mode)
	}

	flags := flag.NewFlagSet("central-sampler ownership "+mode, flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	accountUID := flags.String("account-uid", "", "explicit account ServiceAccount UID")
	manifestPath := flags.String("manifest", "", "JSON file containing the reviewed Session-ID manifest")
	if err := flags.Parse(args[2:]); err != nil {
		return true, err
	}
	if flags.NArg() != 0 {
		return true, errors.New("unexpected positional arguments")
	}
	if strings.TrimSpace(*accountUID) == "" {
		return true, errors.New("--account-uid is required")
	}
	if mode == "inventory" && *manifestPath != "" {
		return true, errors.New("inventory does not accept --manifest")
	}
	if mode != "inventory" && *manifestPath == "" {
		return true, errors.New("--manifest is required for forward and reverse")
	}

	databaseURL := strings.TrimSpace(os.Getenv(ownershipDatabaseURLEnv))
	if databaseURL == "" {
		return true, fmt.Errorf("%s is required", ownershipDatabaseURLEnv)
	}
	databaseURL = strings.Replace(databaseURL, "postgresql+asyncpg://", "postgresql://", 1)
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return true, fmt.Errorf("open PostgreSQL connection: %w", err)
	}
	defer db.Close()
	if err := db.PingContext(ctx); err != nil {
		return true, fmt.Errorf("connect to PostgreSQL: %w", err)
	}

	if mode == "inventory" {
		ids, err := ownershiprewrite.Inventory(ctx, db, *accountUID)
		if err != nil {
			return true, err
		}
		if err := json.NewEncoder(stdout).Encode(ids); err != nil {
			return true, fmt.Errorf("write Session-ID manifest: %w", err)
		}
		return true, nil
	}

	manifestBytes, err := os.ReadFile(*manifestPath)
	if err != nil {
		return true, fmt.Errorf("read Session-ID manifest: %w", err)
	}
	var ids []string
	if err := json.Unmarshal(manifestBytes, &ids); err != nil {
		return true, fmt.Errorf("decode Session-ID manifest as a JSON string array: %w", err)
	}
	if mode == "reverse" {
		err = ownershiprewrite.Reverse(ctx, db, ids, *accountUID)
	} else {
		err = ownershiprewrite.Forward(ctx, db, ids, *accountUID)
	}
	if err != nil {
		return true, err
	}
	_, err = fmt.Fprintf(stdout, "ownership %s verified for %d manifest Sessions\n", mode, len(ids))
	if err != nil {
		return true, fmt.Errorf("write completion message: %w", err)
	}
	return true, nil
}
