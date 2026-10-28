package grpc_test

import (
	"context"
	"testing"

	samplepb "github.com/agentydragon/ducktape/devinfra/x/schema_codegen/protobuf/samplepb"
	"google.golang.org/grpc"
)

type recordingConn struct {
	method string
}

func (c *recordingConn) Invoke(_ context.Context, method string, _ any, reply any, _ ...grpc.CallOption) error {
	c.method = method
	reply.(*samplepb.Sample).SampleId = "fake-response"
	return nil
}

func (*recordingConn) NewStream(context.Context, *grpc.StreamDesc, string, ...grpc.CallOption) (grpc.ClientStream, error) {
	panic("unexpected streaming RPC")
}

var _ grpc.ClientConnInterface = (*recordingConn)(nil)

func TestGeneratedGoGRPCClientUsesGeneratedMethod(t *testing.T) {
	connection := &recordingConn{}
	client := samplepb.NewSampleServiceClient(connection)
	response, err := client.GetSample(context.Background(), &samplepb.GetSampleRequest{SampleId: "requested"})
	if err != nil {
		t.Fatal(err)
	}
	if connection.method != "/schema_codegen.v1.SampleService/GetSample" {
		t.Fatalf("generated method path = %q", connection.method)
	}
	if response.SampleId != "fake-response" {
		t.Fatalf("generated response = %#v", response)
	}
}
