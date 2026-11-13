// A loopback-only diagnostic proxy for native Bazel's accepted-operation recovery.
package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"flag"
	"io"
	"log"
	"net"
	"os"
	"sync/atomic"

	repb "github.com/buildbuddy-io/buildbuddy/proto/remote_execution"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type proxy struct {
	repb.UnimplementedExecutionServer
	repb.UnimplementedCapabilitiesServer
	conn      *grpc.ClientConn
	remaining atomic.Int32
}

func forwarded(ctx context.Context) context.Context {
	md, _ := metadata.FromIncomingContext(ctx)
	return metadata.NewOutgoingContext(ctx, md.Copy())
}

func record(event string, operation string, done bool) {
	// Never record request headers, credentials, action contents, or response payloads.
	if err := json.NewEncoder(os.Stdout).Encode(struct {
		Event     string `json:"event"`
		Operation string `json:"operation,omitempty"`
		Done      bool   `json:"done"`
	}{event, operation, done}); err != nil {
		log.Fatal(err)
	}
}

func (p *proxy) interrupt() bool {
	for {
		remaining := p.remaining.Load()
		if remaining == 0 {
			return false
		}
		if p.remaining.CompareAndSwap(remaining, remaining-1) {
			return true
		}
	}
}

func (p *proxy) GetCapabilities(ctx context.Context, req *repb.GetCapabilitiesRequest) (*repb.ServerCapabilities, error) {
	return repb.NewCapabilitiesClient(p.conn).GetCapabilities(forwarded(ctx), req)
}

func (p *proxy) Execute(req *repb.ExecuteRequest, downstream repb.Execution_ExecuteServer) error {
	record("execute", "", false)
	upstream, err := repb.NewExecutionClient(p.conn).Execute(forwarded(downstream.Context()), req)
	if err != nil {
		return err
	}
	for {
		op, err := upstream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if err := downstream.Send(op); err != nil {
			return err
		}
		if !op.GetDone() && p.interrupt() {
			record("injected_internal_after_operation", op.GetName(), false)
			return status.Error(codes.Internal, "diagnostic: accepted operation stream interrupted")
		}
		record("execute_operation", op.GetName(), op.GetDone())
	}
}

func (p *proxy) WaitExecution(req *repb.WaitExecutionRequest, downstream repb.Execution_WaitExecutionServer) error {
	record("wait_execution", req.GetName(), false)
	upstream, err := repb.NewExecutionClient(p.conn).WaitExecution(forwarded(downstream.Context()), req)
	if err != nil {
		return err
	}
	for {
		op, err := upstream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		record("wait_operation", op.GetName(), op.GetDone())
		if err := downstream.Send(op); err != nil {
			return err
		}
		if !op.GetDone() && p.interrupt() {
			record("injected_internal_after_wait_operation", op.GetName(), false)
			return status.Error(codes.Internal, "diagnostic: accepted operation stream interrupted")
		}
	}
}

func main() {
	address := flag.String("listen", "127.0.0.1:0", "Loopback listener")
	interruptions := flag.Int("interruptions", 1, "Number of nonterminal Execute/WaitExecution responses to interrupt (1 or 2)")
	flag.Parse()
	if *interruptions < 1 || *interruptions > 2 {
		log.Fatal("interruptions must be 1 or 2")
	}
	host, _, err := net.SplitHostPort(*address)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		log.Fatal("listen must use a loopback IP")
	}
	conn, err := grpc.NewClient("remote.buildbuddy.io:443", grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS12})))
	if err != nil {
		log.Fatal(err)
	}
	defer conn.Close()
	listener, err := net.Listen("tcp", *address)
	if err != nil {
		log.Fatal(err)
	}
	record("listening", listener.Addr().String(), false)
	p := &proxy{conn: conn}
	p.remaining.Store(int32(*interruptions))
	server := grpc.NewServer()
	repb.RegisterExecutionServer(server, p)
	repb.RegisterCapabilitiesServer(server, p)
	if err := server.Serve(listener); err != nil {
		log.Fatal(err)
	}
}
