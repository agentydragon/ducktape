package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	executor "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/service"
)

func main() { os.Exit(run(os.Args[1:])) }

func run(args []string) int {
	if len(args) > 0 && args[0] == "companion" {
		return executor.Main(args[1:])
	}
	flags := flag.NewFlagSet("agentplane-execution-service", flag.ContinueOnError)
	path := flags.String("config", "", "execution service configuration JSON")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 64
	}
	if *path == "" || flags.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "--config is required; positional arguments are forbidden")
		return 64
	}
	config, err := service.Load(*path)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 64
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	if err := service.Run(ctx, config); err != nil {
		fmt.Fprintln(os.Stderr, err)
		if errors.Is(err, service.ErrFencedBackendLoss) {
			return 0
		}
		return 1
	}
	return 0
}
