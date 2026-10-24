package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/observability"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})))
	c, err := loadConfig(os.Getenv)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	shutdownTracing, err := observability.Init(ctx, "egress-sidecar")
	if err != nil {
		slog.Error("egress sidecar tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = (&sidecar{config: c}).run(ctx)
	shutdownTracing()
	if err != nil {
		slog.Error("egress sidecar stopped", "error", err)
		os.Exit(1)
	}
}
