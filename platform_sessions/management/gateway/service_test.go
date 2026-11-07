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
	calls     int
	importErr error
	wrongRef  bool
}

func (f *fakeProvider) GetInfo(_ context.Context, _ *providerpb.GetInfoRequest, _ ...grpc.CallOption) (*providerpb.GetInfoResponse, error) {
	return &providerpb.GetInfoResponse{Provider: &providerpb.ProviderInfo{ProviderId: "chatgpt", DisplayName: "ChatGPT", Version: "1", Instances: []*providerpb.ProviderInstanceInfo{{InstanceId: "personal"}}}}, nil
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
	return &providerpb.ImportSessionResponse{Instance: ref, Result: &providerpb.ImportSessionResponse_Installed{Installed: &providerpb.SessionInstalled{}}}, nil
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

func TestUnknownOutcomeDiagnosticsAreBounded(t *testing.T) {
	statusCode := uint32(503)
	value := &providerpb.OperationOutcomeUnknown{Reason: providerpb.UnknownOutcomeReason_UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR, UpstreamHttpStatus: &statusCode}
	if !unknownValid(value) {
		t.Fatal("valid upstream status rejected")
	}
	value.Reason = providerpb.UnknownOutcomeReason_UNKNOWN_OUTCOME_REASON_TIMEOUT
	if unknownValid(value) {
		t.Fatal("status accepted for timeout")
	}
	value.Reason = providerpb.UnknownOutcomeReason_UNKNOWN_OUTCOME_REASON_UPSTREAM_HTTP_ERROR
	statusCode = 99
	if unknownValid(value) {
		t.Fatal("invalid status accepted")
	}
}
