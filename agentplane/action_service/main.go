// Command actions is the Go Action Service, over the Action Service's PostgreSQL database. It serves the standard
// gRPC health service and the process's Prometheus metrics at /metrics on one listener. It does not yet serve the
// Action RPCs or run the dispatch worker.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	_ "github.com/lib/pq"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

const (
	// shutdownGrace bounds how long open calls may finish once the process is told to stop, as the Python
	// service's graceful shutdown does.
	shutdownGrace = 5 * time.Second
	// readHeaderTimeout bounds a request's headers.
	readHeaderTimeout = 10 * time.Second
	// maxConnections and idleConnections size the pool as the Python service's: four kept and two more under load.
	maxConnections  = 6
	idleConnections = 4
)

// run serves on the configured address until ctx ends.
func run(ctx context.Context, c config) error {
	listener, err := net.Listen("tcp", net.JoinHostPort(c.Host, strconv.Itoa(c.Port)))
	if err != nil {
		return err
	}
	defer listener.Close()
	return serve(ctx, c, listener)
}

func openDatabase(ctx context.Context, databaseURL string) (*sql.DB, error) {
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(maxConnections)
	db.SetMaxIdleConns(idleConnections)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("Action Service PostgreSQL unavailable: %w", err)
	}
	return db, nil
}

// serve answers on listener until ctx ends or the listener fails. gRPC and the HTTP routes share the listener.
func serve(ctx context.Context, c config, listener net.Listener) error {
	db, err := openDatabase(ctx, c.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	hub, err := wakeups.Listen(ctx, c.DatabaseURL, store.UpdatesChannel, store.DispatchChannel)
	if err != nil {
		return fmt.Errorf("Action Service PostgreSQL listener unavailable: %w", err)
	}
	defer hub.Close()

	health := probes.New(db, hub)
	grpcServer := grpc.NewServer()
	healthpb.RegisterHealthServer(grpcServer, health)
	routes := http.NewServeMux()
	routes.Handle("/metrics", promhttp.Handler())
	server := &http.Server{Handler: routes, ReadHeaderTimeout: readHeaderTimeout}
	facade.MountGRPC(server, grpcServer)

	failed := make(chan error, 1)
	go func() { failed <- fmt.Errorf("listener: %w", server.Serve(listener)) }()
	var result error
	select {
	case result = <-failed:
	case <-ctx.Done():
		result = ctx.Err()
	}
	health.Drain()
	stopped, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownGrace)
	defer cancel()
	if err := server.Shutdown(stopped); err != nil {
		server.Close()
	}
	return result
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid Action Service configuration", "error", err)
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, c); err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("Action Service stopped", "error", err)
		os.Exit(1)
	}
}
