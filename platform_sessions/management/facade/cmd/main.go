// The facade is an internal HTTP-to-gRPC translator. The public gateway must authenticate requests
// before forwarding them here; both its listener and its gRPC target are restricted to loopback.
package main

import (
	"context"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/platform_sessions/management/facade"
)

func main() {
	target := flag.String("grpc-target", "", "Loopback management gRPC target")
	flag.Parse()
	host, _, err := net.SplitHostPort(*target)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		fmt.Fprintln(os.Stderr, "A loopback management gRPC target is required")
		os.Exit(2)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer cancel()
	handler, err := facade.New(ctx, *target, 30*time.Second)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Could not initialize management facade")
		os.Exit(1)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fmt.Fprintln(os.Stderr, "Could not bind management facade")
		os.Exit(1)
	}
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second}
	fmt.Println(listener.Addr().String())
	go func() {
		<-ctx.Done()
		shutdown, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		server.Shutdown(shutdown)
	}()
	if err := server.Serve(listener); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, "Management facade stopped unexpectedly")
		os.Exit(1)
	}
}
