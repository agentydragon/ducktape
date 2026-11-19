package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/managementpb"
	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/providerpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

type fakeProvider struct {
	providerpb.SessionProviderControlClient
	calls        int
	importErr    error
	wrongRef     bool
	documentPath string
	loginURL     *string
}

func (f *fakeProvider) GetInfo(_ context.Context, _ *providerpb.GetInfoRequest, _ ...grpc.CallOption) (*providerpb.GetInfoResponse, error) {
	instance := &providerpb.ProviderInstanceInfo{InstanceId: "personal", LoginUrl: f.loginURL}
	if f.documentPath != "" {
		instance.OpenapiDocumentPath = &f.documentPath
	}
	return &providerpb.GetInfoResponse{Provider: &providerpb.ProviderInfo{ProviderId: "chatgpt", DisplayName: "ChatGPT", Instances: []*providerpb.ProviderInstanceInfo{instance}}}, nil
}

func (f *fakeProvider) ImportSession(_ context.Context, req *providerpb.ImportSessionRequest, _ ...grpc.CallOption) (*providerpb.ImportSessionResponse, error) {
	f.calls++
	if f.importErr != nil {
		return nil, f.importErr
	}
	ref := req.Instance
	if f.wrongRef {
		ref = &providerpb.InstanceRef{ProviderId: "chatgpt", InstanceId: "wrong"}
	}
	return &providerpb.ImportSessionResponse{Instance: ref, Outcome: &providerpb.ImportSessionResponse_Installed{Installed: &emptypb.Empty{}}}, nil
}

func TestManagementPreservesProviderLoginURL(t *testing.T) {
	loginURL := "https://signin.example.test/auth?next=%2Faccount"
	fake := &fakeProvider{loginURL: &loginURL}
	service, err := New([]Provider{{Ref: &providerpb.InstanceRef{ProviderId: "chatgpt", InstanceId: "personal"}, Client: fake}})
	if err != nil {
		t.Fatal(err)
	}
	list, err := service.ListInstances(context.Background(), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	data, err := protojson.Marshal(list)
	if err != nil {
		t.Fatal(err)
	}
	var body struct {
		Instances []struct {
			Info struct {
				LoginURL string `json:"loginUrl"`
			} `json:"info"`
		} `json:"instances"`
	}
	if err := json.Unmarshal(data, &body); err != nil {
		t.Fatal(err)
	}
	if len(body.Instances) != 1 || body.Instances[0].Info.LoginURL != loginURL {
		t.Fatalf("provider sign-in URL not preserved in management JSON: %s", data)
	}
}

func TestManagementForwardsOnceAndRejectsAmbiguousResults(t *testing.T) {
	fake := &fakeProvider{}
	service, err := New([]Provider{{Ref: &providerpb.InstanceRef{ProviderId: "chatgpt", InstanceId: "personal"}, Client: fake}})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	list, err := service.ListInstances(ctx, &emptypb.Empty{})
	if err != nil || len(list.GetInstances()) != 1 || list.GetInstances()[0].GetInfo().GetInstanceId() != "personal" {
		t.Fatalf("list: %v, %v", list, err)
	}
	req := &managementpb.ImportSessionRequest{InstanceId: "personal", MediaType: "application/json", Candidate: []byte("test")}
	result, err := service.ImportSession(ctx, req)
	if err != nil || result.GetInstalled() == nil || fake.calls != 1 {
		t.Fatalf("import: %v, %v, calls=%d", result, err, fake.calls)
	}
	fake.importErr = errors.New("contains secret, do not expose")
	_, err = service.ImportSession(ctx, req)
	if status.Code(err) != codes.Unknown || fake.calls != 2 || status.Convert(err).Message() == fake.importErr.Error() {
		t.Fatalf("ambiguous outcome: %v, calls=%d", err, fake.calls)
	}
	fake.importErr = nil
	fake.wrongRef = true
	_, err = service.ImportSession(ctx, req)
	if status.Code(err) != codes.Unknown || fake.calls != 3 {
		t.Fatalf("wrong identity: %v", err)
	}
	req.Candidate = nil
	_, err = service.ImportSession(ctx, req)
	if status.Code(err) != codes.InvalidArgument || fake.calls != 3 {
		t.Fatalf("invalid input: %v", err)
	}
}

// The shared protocol examples are JSON and cannot carry an unknown wire field.
func TestFutureImportOutcomeIsInvalid(t *testing.T) {
	// A future oneof field stays unknown and cannot be mistaken for a known successful result.
	future := &providerpb.ImportSessionResponse{}
	if err := proto.Unmarshal([]byte{0xa2, 0x06, 0x00}, future); err != nil {
		t.Fatal(err)
	}
	if importValid(future) {
		t.Fatal("future outcome accepted as a known result")
	}
}
