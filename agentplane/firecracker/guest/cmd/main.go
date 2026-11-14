package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/firecracker/guest"
)

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if err := guest.Run(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "Firecracker thaw agent failed: %v\n", err)
		os.Exit(1)
	}
}
