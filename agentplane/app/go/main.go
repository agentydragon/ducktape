package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/app/go/migrations"
	dbmigrate "github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
	"github.com/go-logr/logr"
	"sigs.k8s.io/controller-runtime/pkg/log"
)

func main() {
	configureLogLevels(nil)
	if len(os.Args) == 2 && os.Args[1] == "--export-openapi" {
		if err := exportOpenAPI(os.Stdout); err != nil {
			slog.Error("export Agentplane OpenAPI schema", "error", err)
			os.Exit(1)
		}
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if handled, err := dbmigrate.RunOnly(ctx, os.Args[1:], "AGENTPLANE_DATABASE_URL", migrations.Up); handled {
		if err != nil {
			slog.Error("migrate Agentplane app database", "error", err)
			os.Exit(1)
		}
		return
	}
	config, err := loadConfigWithArgs(os.Args[1:])
	if err != nil {
		slog.Error("invalid Agentplane app configuration", "error", err)
		os.Exit(1)
	}
	configureLogLevels(config.LogLevels)
	if err := run(ctx, config); err != nil && ctx.Err() == nil {
		slog.Error("Agentplane app stopped", "error", err)
		os.Exit(1)
	}
}

func configureLogLevels(levels map[string]string) {
	level := slog.LevelInfo
	if configured, ok := levels["root"]; ok {
		switch strings.ToUpper(configured) {
		case "DEBUG":
			level = slog.LevelDebug
		case "INFO":
			level = slog.LevelInfo
		case "WARNING", "WARN":
			level = slog.LevelWarn
		case "ERROR", "CRITICAL":
			level = slog.LevelError
		default:
		}
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: level})))
	log.SetLogger(logr.FromSlogHandler(slog.Default().Handler()))
}
