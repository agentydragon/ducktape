package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	vsockfirecracker "github.com/agentydragon/ducktape/util/vsock/firecracker"
)

const (
	vsockPort        = 52
	handshakeTimeout = 5 * time.Second
)

func listenPrivateUnix(path string) (net.Listener, error) {
	listener, err := net.Listen("unix", path)
	if err != nil {
		return nil, fmt.Errorf("listen on local gRPC socket %q: %w", path, err)
	}
	if err := os.Chmod(path, 0o600); err != nil {
		_ = listener.Close()
		return nil, fmt.Errorf("set local gRPC socket permissions: %w", err)
	}
	return listener, nil
}

func run(arguments []string) error {
	flags := flag.NewFlagSet("agentplane-firecracker-vsock-bridge", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	socketPath := flags.String("socket", "", "Firecracker vsock proxy Unix socket")
	listenPath := flags.String("listen", "", "private local Unix socket for gRPC clients")
	if err := flags.Parse(arguments); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments: %q", flags.Args())
	}
	if *socketPath == "" {
		return errors.New("--socket is required")
	}
	if *listenPath == "" {
		return errors.New("--listen is required")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	listener, err := listenPrivateUnix(*listenPath)
	if err != nil {
		return err
	}
	return vsockfirecracker.Serve(ctx, listener, *socketPath, vsockPort, handshakeTimeout)
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "Firecracker vsock bridge failed: %v\n", err)
		os.Exit(1)
	}
}
