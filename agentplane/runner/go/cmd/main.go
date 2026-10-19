package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	runner "github.com/agentydragon/ducktape/agentplane/runner/go"
)

func main() {
	if handled, code := runner.InternalMode(os.Args[1:]); handled {
		os.Exit(code)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if err := runner.CLI(ctx, os.Args[1:]); err != nil {
		slog.Error("runner stopped", "error", err)
		os.Exit(1)
	}
}
