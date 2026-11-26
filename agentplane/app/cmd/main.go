package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/app/internal/migrations"
	"github.com/agentydragon/ducktape/agentplane/app/internal/server"
	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/observability"
	dbmigrate "github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/go-logr/logr"
	"sigs.k8s.io/controller-runtime/pkg/log"
)

func main() {
	configureLogging(slog.LevelInfo)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if handled, err := dbmigrate.RunOnly(ctx, os.Args[1:], "AGENTPLANE_DATABASE_URL", migrations.Up); handled {
		if err != nil {
			slog.Error("migrate Agentplane app database", "error", err)
			os.Exit(1)
		}
		return
	}
	if len(os.Args) != 1 {
		slog.Error("invalid Agentplane app command line", "error", "only --migrate-only is supported")
		os.Exit(1)
	}
	config, err := settings.Load()
	if err != nil {
		slog.Error("invalid Agentplane app configuration", "error", err)
		os.Exit(1)
	}
	level, err := observability.RootLogLevel(config.LogLevels)
	if err != nil {
		slog.Error("invalid Agentplane app configuration", "error", err)
		os.Exit(1)
	}
	configureLogging(level)
	if err := server.Run(ctx, config); err != nil && ctx.Err() == nil {
		slog.Error("Agentplane app stopped", "error", err)
		os.Exit(1)
	}
}

func configureLogging(level slog.Level) {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: level})))
	log.SetLogger(logr.FromSlogHandler(slog.Default().Handler()))
}
