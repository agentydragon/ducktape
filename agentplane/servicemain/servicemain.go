// Package servicemain is the process lifecycle shared by Agentplane service binaries.
package servicemain

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
)

// exitConfig sets an invalid configuration apart from other failures.
const (
	exitFailure = 1
	exitConfig  = 2
)

// Migrations is a service's migration-only invocation (`--migrate-only`).
type Migrations struct {
	DatabaseURLEnv string
	Up             func(context.Context, *sql.DB) error
}

// Service names one binary's steps. Name ("Action Service") starts its log messages; TraceName is its tracing
// service name.
type Service[C any] struct {
	Name       string
	TraceName  string
	Migrations Migrations
	Load       func() (C, error)
	Run        func(context.Context, C) error
}

// Main logs JSON to stderr, ends ctx on SIGINT or SIGTERM, then runs the migration-only invocation if asked;
// otherwise it loads the configuration (exit 2 if invalid), starts tracing and runs the service. A run that ends
// with an error other than cancellation exits 1.
func Main[C any](s Service[C]) {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if handled, err := migrate.RunOnly(ctx, os.Args[1:], s.Migrations.DatabaseURLEnv, s.Migrations.Up); handled {
		if err != nil {
			slog.Error(s.Name+" migration failed", "error", err)
			os.Exit(exitFailure)
		}
		return
	}
	c, err := s.Load()
	if err != nil {
		slog.Error("invalid "+s.Name+" configuration", "error", err)
		os.Exit(exitConfig)
	}
	shutdownTracing, err := observability.Init(ctx, s.TraceName)
	if err != nil {
		slog.Error(s.Name+" tracing setup failed", "error", err)
		os.Exit(exitFailure)
	}
	err = s.Run(ctx, c)
	shutdownTracing()
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Error(s.Name+" stopped", "error", err)
		os.Exit(exitFailure)
	}
}
