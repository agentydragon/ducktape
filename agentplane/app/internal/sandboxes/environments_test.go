package sandboxes

import (
	"context"
	"testing"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
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
	reader, err := NewEnvironmentReader(service)
	if err != nil {
		t.Fatal(err)
	}
	_, err = reader.GetEnvironment(context.Background(), &environmentpb.GetEnvironmentRequest{Target: &identitypb.EnvironmentTarget{Namespace: "team", Name: "worker", Uid: "opaque-uid"}})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("error = %v, want NotFound", err)
	}
	want := &identitypb.EnvironmentTarget{Namespace: "team", Name: "worker", Uid: "opaque-uid"}
	if !proto.Equal(service.request.GetTarget(), want) {
		t.Fatalf("target = %v, want %v", service.request.GetTarget(), want)
	}
}
