// healthprobe checks a service through its verified TLS RPC listener.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"google.golang.org/grpc"
	health "google.golang.org/grpc/health/grpc_health_v1"
)

func run() error {
	target := flag.String("target", "", "RPC host:port")
	caFile := flag.String("ca-file", "", "absolute path to trust roots")
	serverURI := flag.String("server-uri", "", "expected server SPIFFE URI")
	service := flag.String("service", "", "health service name")
	flag.Parse()
	if *target == "" {
		return fmt.Errorf("target is required")
	}
	tls := &transport.ClientTLS{CAFile: *caFile, ServerURI: *serverURI}
	credentials, err := tls.Credentials()
	if err != nil {
		return err
	}
	connection, err := grpc.NewClient(*target, grpc.WithTransportCredentials(credentials), grpc.WithDisableRetry())
	if err != nil {
		return err
	}
	defer connection.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	response, err := health.NewHealthClient(connection).Check(ctx, &health.HealthCheckRequest{Service: *service})
	if err != nil {
		return err
	}
	if response.Status != health.HealthCheckResponse_SERVING {
		return fmt.Errorf("service health: %s", response.Status)
	}
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
