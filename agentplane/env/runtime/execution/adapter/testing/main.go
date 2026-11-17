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

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/adapter"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// bindingExecutor supplies the Environments first-open binding at the direct local-test boundary.
type bindingExecutor struct {
	*adapter.Adapter
	incarnationUID string
}

func (s *bindingExecutor) OpenSession(request *executorpb.OpenSessionRequest, stream grpc.ServerStreamingServer[executorpb.OpenSessionResponse]) error {
	request = proto.Clone(request).(*executorpb.OpenSessionRequest)
	if request.GetScope() == nil {
		return status.Error(codes.InvalidArgument, "scope is required")
	}
	if request.Scope.ExpectedIncarnationUid == "" {
		if request.ResumeSessionId != nil {
			return status.Error(codes.InvalidArgument, "resuming requires an incarnation fence")
		}
		request.Scope.ExpectedIncarnationUid = s.incarnationUID
	}
	return s.Adapter.OpenSession(request, &bindingStream{ServerStreamingServer: stream, incarnationUID: request.Scope.ExpectedIncarnationUid})
}

type bindingStream struct {
	grpc.ServerStreamingServer[executorpb.OpenSessionResponse]
	incarnationUID string
}

func (s *bindingStream) Send(response *executorpb.OpenSessionResponse) error {
	if s.incarnationUID != "" {
		response = proto.Clone(response).(*executorpb.OpenSessionResponse)
		response.IncarnationUid = s.incarnationUID
		s.incarnationUID = ""
	}
	return s.ServerStreamingServer.Send(response)
}

func main() {
	listen := flag.String("listen", "127.0.0.1:0", "explicit insecure loopback gRPC address")
	endpoint := flag.String("executor-url", "", "explicit test Codex exec-server WebSocket endpoint")
	incarnationUID := flag.String("incarnation-uid", "test-pod-uid", "explicit local-test executor incarnation")
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
	executionAdapter, err := adapter.New(adapter.Config{Context: ctx, Admit: func(ctx context.Context, _ *executorpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		return accept(ctx)
	}, Acquire: func(context.Context) (string, error) { return *endpoint, nil }})
	if err != nil {
		log.Fatal(err)
	}
	defer executionAdapter.Close()
	listener, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	server := grpc.NewServer()
	executorpb.RegisterExecutorServer(server, &bindingExecutor{Adapter: executionAdapter, incarnationUID: *incarnationUID})
	go func() { <-ctx.Done(); server.Stop() }()
	fmt.Println(listener.Addr().String())
	if err := server.Serve(listener); err != nil {
		log.Fatal(err)
	}
}
