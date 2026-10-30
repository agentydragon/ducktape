// adapter_bin serves the production typed adapter against an explicit test executor.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net"
	"net/url"
	"os/signal"
	"syscall"

	executionpb "github.com/agentydragon/ducktape/agentplane/proto/executionpb"
	"github.com/agentydragon/ducktape/agentplane/runner/execution"
	runnerexecpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	"google.golang.org/grpc"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:0", "explicit insecure loopback gRPC address")
	endpoint := flag.String("executor-url", "", "explicit test Codex exec-server WebSocket endpoint")
	flag.Parse()
	host, _, err := net.SplitHostPort(*listen)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		log.Fatal("--listen must be an IP loopback address")
	}
	parsed, err := url.Parse(*endpoint)
	if err != nil || parsed.Scheme != "ws" || net.ParseIP(parsed.Hostname()) == nil || !net.ParseIP(parsed.Hostname()).IsLoopback() {
		log.Fatal("--executor-url must be an explicit ws:// loopback test endpoint")
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	adapter, err := execution.New(execution.Config{Context: ctx, Admit: func(ctx context.Context, _ *executionpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		return accept(ctx)
	}, Acquire: func(context.Context) (string, error) { return *endpoint, nil }})
	if err != nil {
		log.Fatal(err)
	}
	defer adapter.Close()
	listener, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	server := grpc.NewServer()
	runnerexecpb.RegisterRunnerExecServer(server, adapter)
	go func() { <-ctx.Done(); server.Stop() }()
	fmt.Println(listener.Addr().String())
	if err := server.Serve(listener); err != nil {
		log.Fatal(err)
	}
}
