// Package grpcmethods calls every RPC a gRPC service declares by name, so a test can check a server's caller
// policy (auth.Methods) method by method without building each method's request.
package grpcmethods

import (
	"context"
	"errors"
	"io"
	"net"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/emptypb"
)

// Method is one RPC of a service.
type Method struct {
	// Name is the full method name, `/package.Service/Method`.
	Name   string
	stream *grpc.StreamDesc
}

// Of lists every RPC desc declares.
func Of(desc grpc.ServiceDesc) []Method {
	var methods []Method
	for _, method := range desc.Methods {
		methods = append(methods, Method{Name: "/" + desc.ServiceName + "/" + method.MethodName})
	}
	for _, stream := range desc.Streams {
		methods = append(methods, Method{Name: "/" + desc.ServiceName + "/" + stream.StreamName, stream: &stream})
	}
	return methods
}

// Call makes the RPC with an empty request and returns its error; a stream's is that of its first receive. A
// server admits a call before its handler reads the request, so the error shows how the policy treats the caller.
func (m Method) Call(ctx context.Context, conn grpc.ClientConnInterface) error {
	if m.stream == nil {
		return conn.Invoke(ctx, m.Name, &emptypb.Empty{}, &emptypb.Empty{})
	}
	stream, err := conn.NewStream(ctx, m.stream, m.Name)
	if err != nil {
		return err
	}
	// A refused stream ends the send side with io.EOF; its status comes with the receive.
	if err := stream.SendMsg(&emptypb.Empty{}); err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	if err := stream.CloseSend(); err != nil {
		return err
	}
	return stream.RecvMsg(&emptypb.Empty{})
}

// Serve serves the services register adds behind methods on a loopback port until the test ends, and returns a
// connection to it.
func Serve(t *testing.T, methods auth.Methods, register func(*grpc.Server)) *grpc.ClientConn {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(methods.ServerOptions(0)...)
	register(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return connection
}
