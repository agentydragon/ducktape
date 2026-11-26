// Package servicecmd owns the standard startup path for migration-backed Agentplane services.
package servicecmd

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
)

type Command[Config any] struct {
	Name           string
	TraceName      string
	DatabaseURLEnv string
	Migrate        func(context.Context, *sql.DB) error
	LoadConfig     func() (Config, error)
	Serve          func(context.Context, Config) error
}

// Run handles migration-only mode before loading serving settings.
// Serve owns normal startup migrations and the service's shutdown sequence.
func (c Command[Config]) Run(ctx context.Context, args []string) int {
	if handled, err := migrate.RunOnly(ctx, args, c.DatabaseURLEnv, c.Migrate); handled {
		if err != nil {
			slog.Error(c.Name+" migration failed", "error", err)
			return 1
		}
		return 0
	}
	config, err := c.LoadConfig()
	if err != nil {
		slog.Error("invalid "+c.Name+" configuration", "error", err)
		return 2
	}
	shutdownTracing, err := observability.Init(ctx, c.TraceName)
	if err != nil {
		slog.Error(c.Name+" tracing setup failed", "error", err)
		return 1
	}
	err = c.Serve(ctx, config)
	shutdownTracing()
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Error(c.Name+" stopped", "error", err)
		return 1
	}
	return 0
}
