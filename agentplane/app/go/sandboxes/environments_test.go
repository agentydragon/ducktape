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
	pages       map[string]*environmentpb.ListEnvironmentsResponse
	listTokens  []string
	getRequests []*environmentpb.GetEnvironmentRequest
	getResponse *environmentpb.EnvironmentDescriptor
	getError    error
}

func (f *fakeEnvironmentService) ListEnvironments(_ context.Context, request *environmentpb.ListEnvironmentsRequest, _ ...grpc.CallOption) (*environmentpb.ListEnvironmentsResponse, error) {
	f.listTokens = append(f.listTokens, request.GetPageToken())
	return f.pages[request.GetPageToken()], nil
}

func (f *fakeEnvironmentService) GetEnvironment(_ context.Context, request *environmentpb.GetEnvironmentRequest, _ ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error) {
	f.getRequests = append(f.getRequests, proto.Clone(request).(*environmentpb.GetEnvironmentRequest))
	return f.getResponse, f.getError
}

func descriptor(namespace, name, uid string) *environmentpb.EnvironmentDescriptor {
	return &environmentpb.EnvironmentDescriptor{Target: &environmentpb.TargetHandle{Namespace: namespace, Name: name, Uid: uid}}
}

func TestEnvironmentReaderResolvesNameAcrossPagesThenGetsExactUID(t *testing.T) {
	service := &fakeEnvironmentService{
		pages: map[string]*environmentpb.ListEnvironmentsResponse{
			"":     {Items: []*environmentpb.EnvironmentDescriptor{descriptor("other", "worker", "other-uid")}, NextPageToken: "next"},
			"next": {Items: []*environmentpb.EnvironmentDescriptor{descriptor("team", "worker", "target-uid"), descriptor("team", "another", "another-uid")}},
		},
		getResponse: descriptor("team", "worker", "target-uid"),
	}
	reader, err := NewEnvironmentReader("team", service)
	if err != nil {
		t.Fatal(err)
	}
	got, err := reader.Get(context.Background(), "worker")
	if err != nil {
		t.Fatal(err)
	}
	if got.GetTarget().GetUid() != "target-uid" {
		t.Fatalf("Get(worker) target = %v", got.GetTarget())
	}
	if len(service.listTokens) != 2 || service.listTokens[0] != "" || service.listTokens[1] != "next" {
		t.Fatalf("ListEnvironments page tokens = %v", service.listTokens)
	}
	if len(service.getRequests) != 1 || !proto.Equal(service.getRequests[0].GetTarget(), &environmentpb.TargetHandle{Namespace: "team", Name: "worker", Uid: "target-uid"}) {
		t.Fatalf("GetEnvironment target requests = %v", service.getRequests)
	}
}

func TestEnvironmentReaderPropagatesReplacementAndDoesNotResolveAgain(t *testing.T) {
	service := &fakeEnvironmentService{
		pages: map[string]*environmentpb.ListEnvironmentsResponse{
			"": {Items: []*environmentpb.EnvironmentDescriptor{descriptor("team", "worker", "old-uid")}},
		},
		getError: status.Error(codes.NotFound, "requested target UID is no longer current"),
	}
	reader, err := NewEnvironmentReader("team", service)
	if err != nil {
		t.Fatal(err)
	}
	_, err = reader.Get(context.Background(), "worker")
	if status.Code(err) != codes.NotFound {
		t.Fatalf("Get(worker) error = %v, want exact-target NotFound", err)
	}
	if len(service.listTokens) != 1 || len(service.getRequests) != 1 || service.getRequests[0].GetTarget().GetUid() != "old-uid" {
		t.Fatalf("reader re-resolved a stale target: list=%v get=%v", service.listTokens, service.getRequests)
	}
}

func TestEnvironmentReaderGetExactRequiresConfiguredNamespaceAndUID(t *testing.T) {
	service := &fakeEnvironmentService{}
	reader, err := NewEnvironmentReader("team", service)
	if err != nil {
		t.Fatal(err)
	}
	for _, target := range []*environmentpb.TargetHandle{
		{Namespace: "other", Name: "worker", Uid: "uid"},
		{Namespace: "team", Name: "worker"},
	} {
		if _, err := reader.GetExact(context.Background(), target); status.Code(err) != codes.InvalidArgument {
			t.Errorf("GetExact(%v) error = %v, want InvalidArgument", target, err)
		}
	}
	if len(service.getRequests) != 0 {
		t.Fatalf("invalid targets reached Environment Service: %v", service.getRequests)
	}
}
