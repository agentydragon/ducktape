package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"google.golang.org/grpc"
)

type readyMessage struct {
	Address          string `json:"address"`
	RootCertificates string `json:"root_certificates"`
}

func run() error {
	target := flag.String("target", "", "loopback address from kubectl port-forward")
	caFile := flag.String("ca-file", "", "CA bundle used to verify the Dispatcher SVID")
	flag.Parse()
	if *target == "" || *caFile == "" {
		return errors.New("both --target and --ca-file are required")
	}

	proxy, err := newDispatcherProxy(*target, *caFile)
	if err != nil {
		return err
	}
	defer proxy.close()

	serveDone := make(chan error, 1)
	go func() { serveDone <- proxy.server.Serve(proxy.listener) }()
	if err := json.NewEncoder(os.Stdout).Encode(readyMessage{
		Address:          proxy.listener.Addr().String(),
		RootCertificates: string(proxy.rootCertificates),
	}); err != nil {
		return fmt.Errorf("report proxy readiness: %w", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	select {
	case err := <-serveDone:
		if errors.Is(err, grpc.ErrServerStopped) {
			return nil
		}
		return fmt.Errorf("serve local Dispatcher proxy: %w", err)
	case <-ctx.Done():
		proxy.server.Stop()
		err := <-serveDone
		if err != nil && !errors.Is(err, grpc.ErrServerStopped) {
			return fmt.Errorf("stop local Dispatcher proxy: %w", err)
		}
		return nil
	}
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
