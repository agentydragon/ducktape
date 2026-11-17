package gateway

import (
	"context"
	"errors"
	"testing"
	"time"

	managementpb "github.com/agentydragon/ducktape/platform_sessions/management/managementpb"
	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/providerpb"
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
	return &providerpb.GetInfoResponse{Provider: &providerpb.ProviderInfo{ProviderId: "chatgpt", DisplayName: "ChatGPT", Instances: []*providerpb.ProviderInstanceInfo{instance}}}, nil
}

func (f *fakeProvider) ImportSession(_ context.Context, req *providerpb.ImportSessionRequest, _ ...grpc.CallOption) (*providerpb.ImportOperation, error) {
	f.calls++
	if f.importErr != nil {
		return nil, f.importErr
	}
	ref := req.Instance
	if f.wrongRef {
		ref = &providerpb.InstanceRef{ProviderId: "chatgpt", InstanceId: "wrong"}
	}
	return &providerpb.ImportOperation{Instance: ref, OperationId: req.OperationId, State: providerpb.ImportOperationState_IMPORT_OPERATION_STATE_COMPLETED, Result: &providerpb.ImportSessionResponse{Instance: ref, Outcome: providerpb.ImportOutcome_IMPORT_OUTCOME_INSTALLED}}, nil
}

func (f *fakeProvider) GetImportOperation(_ context.Context, req *providerpb.GetImportOperationRequest, _ ...grpc.CallOption) (*providerpb.ImportOperation, error) {
	return &providerpb.ImportOperation{Instance: req.Instance, OperationId: req.OperationId, State: providerpb.ImportOperationState_IMPORT_OPERATION_STATE_PENDING}, nil
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
	req := &managementpb.CandidateRequest{InstanceId: "personal", MediaType: "application/json", FormatCandidate: []byte("test"), OperationId: "f453060d-8836-4a3e-b4b4-68f6ee6e6035"}
	result, err := service.ImportSession(ctx, req)
	if err != nil || result.GetResult().GetOutcome() != providerpb.ImportOutcome_IMPORT_OUTCOME_INSTALLED || fake.calls != 1 {
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

func TestDefaultProviderTimeouts(t *testing.T) {
	timeouts := DefaultProviderTimeouts()
	if timeouts.Status != 5*time.Second || timeouts.Operation != 120*time.Second {
		t.Fatalf("provider deadlines: %+v", timeouts)
	}
}

func TestGetImportOperationIsScopedAndValidatesItsIdentity(t *testing.T) {
	fake := &fakeProvider{}
	ref := &providerpb.InstanceRef{ProviderId: "chatgpt", InstanceId: "personal"}
	service, err := New([]Provider{{Ref: ref, Client: fake}})
	if err != nil {
		t.Fatal(err)
	}
	id := "f453060d-8836-4a3e-b4b4-68f6ee6e6035"
	result, err := service.GetImportOperation(context.Background(), &managementpb.ImportOperationRequest{InstanceId: "personal", OperationId: id})
	if err != nil || result.GetState() != providerpb.ImportOperationState_IMPORT_OPERATION_STATE_PENDING {
		t.Fatalf("lookup: %v, %v", result, err)
	}
	_, err = service.GetImportOperation(context.Background(), &managementpb.ImportOperationRequest{InstanceId: "personal", OperationId: "bad"})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("invalid id: %v", err)
	}
	result.OperationId = "different"
	if validImportOperation(result, ref, id) {
		t.Fatal("accepted operation with wrong identity")
	}
}
