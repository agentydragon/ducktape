package sandboxes

import (
	"context"
	"testing"

	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type fakeEnvironmentService struct {
	request *environmentpb.GetEnvironmentRequest
	err     error
}

func (f *fakeEnvironmentService) GetEnvironment(_ context.Context, request *environmentpb.GetEnvironmentRequest, _ ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error) {
	f.request = proto.Clone(request).(*environmentpb.GetEnvironmentRequest)
	return nil, f.err
}

func TestEnvironmentReaderForwardsExactTargetAndServiceError(t *testing.T) {
	service := &fakeEnvironmentService{err: status.Error(codes.NotFound, "target replaced")}
	reader, err := NewEnvironmentReader("team", service)
	if err != nil {
		t.Fatal(err)
	}
	_, err = reader.GetExactUID(context.Background(), "worker", "opaque-uid")
	if status.Code(err) != codes.NotFound {
		t.Fatalf("error = %v, want NotFound", err)
	}
	want := &environmentpb.TargetHandle{Namespace: "team", Name: "worker", Uid: "opaque-uid"}
	if !proto.Equal(service.request.GetTarget(), want) {
		t.Fatalf("target = %v, want %v", service.request.GetTarget(), want)
	}
}
