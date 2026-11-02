package sandboxes

import (
	"context"
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type fakeEnvironmentService struct {
	request       *environmentpb.GetEnvironmentRequest
	err           error
	watchRequests chan *environmentpb.WatchEnvironmentRequest
}

func (f *fakeEnvironmentService) GetEnvironment(_ context.Context, request *environmentpb.GetEnvironmentRequest, _ ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error) {
	f.request = proto.Clone(request).(*environmentpb.GetEnvironmentRequest)
	return nil, f.err
}

func (f *fakeEnvironmentService) WatchEnvironment(_ context.Context, request *environmentpb.WatchEnvironmentRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[environmentpb.WatchEnvironmentResponse], error) {
	if f.watchRequests != nil {
		f.watchRequests <- proto.Clone(request).(*environmentpb.WatchEnvironmentRequest)
	}
	return nil, f.err
}

func TestEnvironmentWatchUsesExactTargetAndSignalsFailure(t *testing.T) {
	service := &fakeEnvironmentService{err: status.Error(codes.Unavailable, "watch disconnected"), watchRequests: make(chan *environmentpb.WatchEnvironmentRequest, 1)}
	reader, err := NewEnvironmentReader("team", service)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	changes := reader.WatchChanges(ctx, "worker", "opaque-uid")
	select {
	case request := <-service.watchRequests:
		want := &environmentpb.TargetHandle{Namespace: "team", Name: "worker", Uid: "opaque-uid"}
		if !proto.Equal(request.GetTarget(), want) {
			t.Fatalf("watch target = %v, want %v", request.GetTarget(), want)
		}
	case <-time.After(time.Second):
		t.Fatal("watch was not started")
	}
	select {
	case <-changes:
	case <-time.After(time.Second):
		t.Fatal("watch failure did not trigger a refresh")
	}
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
