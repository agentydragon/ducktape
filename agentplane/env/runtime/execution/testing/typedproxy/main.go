// typedproxy_bin serves the native executor through the production typed proxy.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net"
	"os/signal"
	"syscall"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/typedbackend"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/typedproxy"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// bindingExecutor supplies the Environments first-open binding at the direct local-test boundary.
type bindingExecutor struct {
	*typedproxy.Proxy
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
	return s.Proxy.OpenSession(request, &bindingStream{ServerStreamingServer: stream, incarnationUID: request.Scope.ExpectedIncarnationUid})
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

	incarnationUID := flag.String("incarnation-uid", "test-pod-uid", "explicit local-test executor incarnation")
	flag.Parse()
	host, _, err := net.SplitHostPort(*listen)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		log.Fatal("--listen must be an IP loopback address")
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	core := native.New()
	defer core.Close()
	backendListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		log.Fatal(err)
	}
	backend := grpc.NewServer()
	executorpb.RegisterExecutorServer(backend, typedbackend.New(core))
	go func() { _ = backend.Serve(backendListener) }()
	defer backend.Stop()
	connection, err := grpc.NewClient(backendListener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatal(err)
	}
	defer connection.Close()
	proxy, err := typedproxy.New(ctx, executorpb.NewExecutorClient(connection), func(ctx context.Context, _ *executorpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
		return accept(ctx)
	}, nil)
	if err != nil {
		log.Fatal(err)
	}
	defer proxy.Close()
	listener, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	server := grpc.NewServer()
	executorpb.RegisterExecutorServer(server, &bindingExecutor{Proxy: proxy, incarnationUID: *incarnationUID})
	go func() { <-ctx.Done(); server.Stop() }()
	fmt.Println(listener.Addr().String())
	if err := server.Serve(listener); err != nil {
		log.Fatal(err)
	}
}
