package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/observability"
	runner "github.com/agentydragon/ducktape/agentplane/runner"
)

func main() {
	if handled, code := runner.InternalMode(os.Args[1:]); handled {
		os.Exit(code)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	shutdownTracing, err := observability.Init(ctx, "runner")
	if err != nil {
		slog.Error("runner tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = runner.CLI(ctx, os.Args[1:])
	shutdownTracing()
	if err != nil {
		slog.Error("runner stopped", "error", err)
		os.Exit(1)
	}
}
