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
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestSnapshotVariants(t *testing.T) {
	for _, test := range []struct {
		name  string
		value *providerpb.SessionSnapshot
		valid bool
	}{
		{"missing status", &providerpb.SessionSnapshot{}, false},
		{"known missing time", &providerpb.SessionSnapshot{Status: &providerpb.SessionSnapshot_Known{Known: &providerpb.KnownSession{State: providerpb.SessionState_SESSION_STATE_VERIFIED}}}, false},
		{"known", &providerpb.SessionSnapshot{Status: &providerpb.SessionSnapshot_Known{Known: &providerpb.KnownSession{State: providerpb.SessionState_SESSION_STATE_VERIFIED, StateChangedAt: timestamppb.Now()}}}, true},
		{"storage error", &providerpb.SessionSnapshot{Status: &providerpb.SessionSnapshot_StorageError{StorageError: &providerpb.SessionStorageError{}}}, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := snapshotValid(test.value); got != test.valid {
				t.Fatalf("snapshotValid() = %t, want %t", got, test.valid)
			}
		})
	}
}

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
	return &providerpb.ImportOperation{
		Instance: ref, OperationId: req.OperationId,
		State: &providerpb.ImportOperation_Completed{Completed: &providerpb.ImportSessionResponse{
			Instance: ref, Outcome: &providerpb.ImportSessionResponse_Installed{Installed: &emptypb.Empty{}},
		}},
	}, nil
}

func (f *fakeProvider) GetImportOperation(_ context.Context, req *providerpb.GetImportOperationRequest, _ ...grpc.CallOption) (*providerpb.ImportOperation, error) {
	return &providerpb.ImportOperation{Instance: req.Instance, OperationId: req.OperationId, State: &providerpb.ImportOperation_Pending{Pending: &emptypb.Empty{}}}, nil
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
	req := &managementpb.CandidateRequest{InstanceId: "personal", MediaType: "application/json", Candidate: []byte("test"), OperationId: "f453060d-8836-4a3e-b4b4-68f6ee6e6035"}
	result, err := service.ImportSession(ctx, req)
	if err != nil || result.GetCompleted().GetInstalled() == nil || fake.calls != 1 {
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

func TestImportOutcomeOneofRequiresValidPayload(t *testing.T) {
	value := &providerpb.ImportSessionResponse{}
	if importValid(value) {
		t.Fatal("missing outcome accepted")
	}
	value.Outcome = &providerpb.ImportSessionResponse_OutcomeUnknown{OutcomeUnknown: &providerpb.OperationOutcomeUnknown{CredentialRotationMayHaveOccurred: true}}
	if !importValid(value) {
		t.Fatal("unknown outcome with rotation details rejected")
	}
	invalidStatus := uint32(999)
	value.Outcome = &providerpb.ImportSessionResponse_OutcomeUnknown{OutcomeUnknown: &providerpb.OperationOutcomeUnknown{
		Reason:             providerpb.UnknownOutcomeReason_UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR,
		UpstreamHttpStatus: &invalidStatus,
	}}
	if importValid(value) {
		t.Fatal("invalid unknown-outcome HTTP status accepted")
	}

	// A future oneof field stays unknown and cannot be mistaken for a known successful result.
	future := &providerpb.ImportSessionResponse{}
	if err := proto.Unmarshal([]byte{0xa2, 0x06, 0x00}, future); err != nil {
		t.Fatal(err)
	}
	if importValid(future) {
		t.Fatal("future outcome accepted as a known result")
	}
}

func TestImportOperationOneofRequiresExactlyOneKnownState(t *testing.T) {
	ref := &providerpb.InstanceRef{ProviderId: "chatgpt", InstanceId: "personal"}
	const id = "f453060d-8836-4a3e-b4b4-68f6ee6e6035"
	value := &providerpb.ImportOperation{Instance: ref, OperationId: id}
	if validImportOperation(value, ref, id) {
		t.Fatal("missing operation state accepted")
	}
	value.State = &providerpb.ImportOperation_Pending{Pending: &emptypb.Empty{}}
	if !validImportOperation(value, ref, id) {
		t.Fatal("pending operation rejected")
	}
	value.State = &providerpb.ImportOperation_Unknown{Unknown: &emptypb.Empty{}}
	if !validImportOperation(value, ref, id) {
		t.Fatal("unknown operation rejected")
	}
	value.State = &providerpb.ImportOperation_Completed{Completed: &providerpb.ImportSessionResponse{
		Instance: ref, Outcome: &providerpb.ImportSessionResponse_Installed{Installed: &emptypb.Empty{}},
	}}
	if !validImportOperation(value, ref, id) {
		t.Fatal("completed operation rejected")
	}
	future := &providerpb.ImportOperation{Instance: ref, OperationId: id}
	if err := proto.Unmarshal([]byte{0xa2, 0x06, 0x00}, future); err != nil {
		t.Fatal(err)
	}
	if validImportOperation(future, ref, id) {
		t.Fatal("future operation state accepted as a known state")
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
	if err != nil || result.GetPending() == nil {
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
