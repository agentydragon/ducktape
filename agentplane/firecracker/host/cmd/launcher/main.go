package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/firecracker/host"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	syscall.Umask(0077)
	configPath := flag.String("config", "/run/agentplane/firecracker/config.json", "launcher configuration JSON")
	flag.Parse()
	config, err := host.LoadConfig(*configPath)
	if err != nil {
		slog.Error("load launcher configuration", "error", err)
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	supervisor, err := host.NewSupervisor(config)
	if err != nil {
		slog.Error("initialize launcher", "error", err)
		os.Exit(1)
	}
	if err := supervisor.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("launcher stopped", "error", err)
		os.Exit(1)
	}
}
