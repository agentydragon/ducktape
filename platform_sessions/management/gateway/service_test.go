package gateway

import (
	"context"
	"errors"
	"testing"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/v1/managementpb"
	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/v1/providerpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

type fakeProvider struct {
	providerpb.SessionProviderControlClient
	calls        int
	importErr    error
	wrongRef     bool
	documentPath string
}

func (f *fakeProvider) GetInfo(_ context.Context, _ *providerpb.GetInfoRequest, _ ...grpc.CallOption) (*providerpb.GetInfoResponse, error) {
	instance := &providerpb.ProviderInstanceInfo{InstanceId: "personal"}
	if f.documentPath != "" {
		instance.OpenapiDocumentPath = &f.documentPath
	}
	return &providerpb.GetInfoResponse{Provider: &providerpb.ProviderInfo{ProviderId: "chatgpt", DisplayName: "ChatGPT", Version: "1", Instances: []*providerpb.ProviderInstanceInfo{instance}}}, nil
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
	return &providerpb.ImportSessionResponse{Instance: ref, Outcome: providerpb.ImportOutcome_IMPORT_OUTCOME_INSTALLED}, nil
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
	req := &managementpb.CandidateRequest{InstanceId: "personal", MediaType: "application/json", FormatVersion: "1", Candidate: []byte("test")}
	result, err := service.ImportSession(ctx, req)
	if err != nil || result.GetOutcome() != providerpb.ImportOutcome_IMPORT_OUTCOME_INSTALLED || fake.calls != 1 {
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

func TestUnknownOutcomeRequiresRotationDetails(t *testing.T) {
	value := &providerpb.ImportSessionResponse{Outcome: providerpb.ImportOutcome_IMPORT_OUTCOME_UNKNOWN}
	if importValid(value) {
		t.Fatal("unknown outcome accepted without details")
	}
	value.OutcomeUnknown = &providerpb.OperationOutcomeUnknown{CredentialRotationMayHaveOccurred: true}
	if !importValid(value) {
		t.Fatal("unknown outcome with rotation details rejected")
	}
	value.Outcome = providerpb.ImportOutcome(999)
	if !importValid(value) {
		t.Fatal("future outcome rejected")
	}
}
