package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/env/firecracker"
)

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if err := firecracker.Run(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "Firecracker thaw agent failed: %v\n", err)
		os.Exit(1)
	}
}
