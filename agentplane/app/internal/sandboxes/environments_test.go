package sandboxes

import (
	"context"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type fakeEnvironmentService struct {
	request       *environmentpb.GetEnvironmentRequest
	err           error
	watchRequests chan *environmentpb.WatchEnvironmentRequest
	stream        grpc.ServerStreamingClient[environmentpb.WatchEnvironmentResponse]
}

func (f *fakeEnvironmentService) GetEnvironment(_ context.Context, request *environmentpb.GetEnvironmentRequest, _ ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error) {
	f.request = proto.Clone(request).(*environmentpb.GetEnvironmentRequest)
	return nil, f.err
}

type environmentStream struct {
	grpc.ServerStreamingClient[environmentpb.WatchEnvironmentResponse]
	frames []*environmentpb.WatchEnvironmentResponse
}

func (s *environmentStream) Recv() (*environmentpb.WatchEnvironmentResponse, error) {
	if len(s.frames) == 0 {
		return nil, status.Error(codes.NotFound, "target replaced")
	}
	frame := s.frames[0]
	s.frames = s.frames[1:]
	return frame, nil
}

func TestEnvironmentWatchDeliversSnapshotsAndTerminalErrorWithoutGet(t *testing.T) {
	target := &identitypb.EnvironmentTarget{Namespace: "team", Name: "worker", Uid: "uid"}
	descriptor := &environmentpb.EnvironmentDescriptor{Target: target}
	service := &fakeEnvironmentService{stream: &environmentStream{frames: []*environmentpb.WatchEnvironmentResponse{{Environment: descriptor}}}}
	reader, err := NewEnvironmentReader(service)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	observations := reader.Watch(ctx, target)
	first := <-observations
	if first.Err != nil || !proto.Equal(first.Descriptor, descriptor) {
		t.Fatalf("watch snapshot = %v, %v", first.Descriptor, first.Err)
	}
	last := <-observations
	if status.Code(last.Err) != codes.NotFound || service.request != nil {
		t.Fatalf("watch terminal error = %v, get request = %v", last.Err, service.request)
	}
	if _, ok := <-observations; ok {
		t.Fatal("watch did not close after terminal error")
	}
}

func (f *fakeEnvironmentService) WatchEnvironment(_ context.Context, request *environmentpb.WatchEnvironmentRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[environmentpb.WatchEnvironmentResponse], error) {
	if f.watchRequests != nil {
		f.watchRequests <- proto.Clone(request).(*environmentpb.WatchEnvironmentRequest)
	}
	return f.stream, f.err
}

func TestEnvironmentWatchUsesExactTargetAndSignalsFailure(t *testing.T) {
	service := &fakeEnvironmentService{err: status.Error(codes.Unavailable, "watch disconnected"), watchRequests: make(chan *environmentpb.WatchEnvironmentRequest, 1)}
	reader, err := NewEnvironmentReader(service)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	changes := reader.Watch(ctx, &identitypb.EnvironmentTarget{Namespace: "team", Name: "worker", Uid: "opaque-uid"})
	select {
	case request := <-service.watchRequests:
		want := &identitypb.EnvironmentTarget{Namespace: "team", Name: "worker", Uid: "opaque-uid"}
		if !proto.Equal(request.GetTarget(), want) {
			t.Fatalf("watch target = %v, want %v", request.GetTarget(), want)
		}
	case <-time.After(time.Second):
		t.Fatal("watch was not started")
	}
	select {
	case observation := <-changes:
		if status.Code(observation.Err) != codes.Unavailable {
			t.Fatalf("watch error = %v", observation.Err)
		}
	case <-time.After(time.Second):
		t.Fatal("watch failure did not trigger a refresh")
	}
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
