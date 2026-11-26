package actions

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
)

const testSessionID = "018f4e32-6632-7e7f-83dd-7f90ccf84f68"

type callRecord struct {
	peer     string
	envelope *delegationpb.Delegation
}

type actionsService struct {
	action_servicepb.UnimplementedActionServiceServer

	mu             sync.Mutex
	records        []callRecord
	submissions    int
	listKeys       []string
	watchCalls     int
	watchRetry     bool
	watchStarted   chan struct{}
	releaseWatch   chan struct{}
	catalogWait    <-chan struct{}
	catalogEntered chan struct{}
}

func (s *actionsService) record(ctx context.Context) error {
	peerIdentity, err := peertls.PeerIdentity(ctx)
	if err != nil {
		return err
	}
	md, _ := metadata.FromIncomingContext(ctx)
	values := md.Get(auth.DelegationMetadataKey)
	if len(values) != 1 {
		return status.Errorf(codes.Unauthenticated, "got %d delegation envelopes", len(values))
	}
	envelope := new(delegationpb.Delegation)
	if err := proto.Unmarshal([]byte(values[0]), envelope); err != nil {
		return status.Error(codes.Unauthenticated, "invalid delegation envelope")
	}
	if len(md.Get("authorization")) != 0 {
		return status.Error(codes.Unauthenticated, "unexpected authorization metadata")
	}
	s.mu.Lock()
	s.records = append(s.records, callRecord{peer: peerIdentity, envelope: envelope})
	s.mu.Unlock()
	return nil
}

func (s *actionsService) ListActionGroups(ctx context.Context, _ *action_servicepb.ListActionGroupsRequest) (*action_servicepb.ListActionGroupsResponse, error) {
	if err := s.record(ctx); err != nil {
		return nil, err
	}
	if s.catalogEntered != nil {
		select {
		case s.catalogEntered <- struct{}{}:
		default:
		}
	}
	if s.catalogWait != nil {
		select {
		case <-s.catalogWait:
		case <-ctx.Done():
			return nil, status.FromContextError(ctx.Err()).Err()
		}
	}
	return &action_servicepb.ListActionGroupsResponse{ActionGroups: []*action_servicepb.ActionGroup{{Key: "test-group"}}}, nil
}

func (s *actionsService) SubmitActionRequest(ctx context.Context, request *action_servicepb.SubmitActionRequestRequest) (*action_servicepb.SubmitActionRequestResponse, error) {
	if err := s.record(ctx); err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.submissions++
	s.mu.Unlock()
	return &action_servicepb.SubmitActionRequestResponse{ActionRequest: &action_servicepb.ActionRequest{
		Id: "018f4e32-6632-7e7f-83dd-7f90ccf84f69", IdempotencyKey: request.GetIdempotencyKey(),
		Arguments: proto.Clone(request.GetArguments()).(*structpb.Struct), State: action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING,
	}}, nil
}

func (s *actionsService) ListActionRequests(ctx context.Context, request *action_servicepb.ListActionRequestsRequest) (*action_servicepb.ListActionRequestsResponse, error) {
	if err := s.record(ctx); err != nil {
		return nil, err
	}
	key := request.GetIdempotencyKey()
	s.mu.Lock()
	s.listKeys = append(s.listKeys, key)
	s.mu.Unlock()
	return &action_servicepb.ListActionRequestsResponse{ActionRequests: []*action_servicepb.ActionRequest{{
		Id: "018f4e32-6632-7e7f-83dd-7f90ccf84f69", IdempotencyKey: key,
		State: action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING,
	}}}, nil
}

func (s *actionsService) WatchActionRequest(request *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
	if err := s.record(stream.Context()); err != nil {
		return err
	}
	if request.GetUntil() != action_servicepb.WatchUntil_WATCH_UNTIL_TERMINAL {
		return status.Error(codes.InvalidArgument, "watch must request terminal state")
	}
	s.mu.Lock()
	s.watchCalls++
	call := s.watchCalls
	s.mu.Unlock()
	pending := &action_servicepb.ActionRequest{Id: request.GetRequestId(), State: action_servicepb.ActionState_ACTION_STATE_RUNNING}
	if err := stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: pending}); err != nil {
		return err
	}
	if s.watchRetry && call == 1 {
		return status.Error(codes.Unavailable, "transient watch interruption")
	}
	if s.watchStarted != nil {
		select {
		case s.watchStarted <- struct{}{}:
		default:
		}
	}
	if s.releaseWatch != nil {
		select {
		case <-s.releaseWatch:
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}
	// An older Sampler can receive a state added by a newer Action Service.
	future := proto.Clone(pending).(*action_servicepb.ActionRequest)
	future.State = action_servicepb.ActionState(99)
	if err := stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: future}); err != nil {
		return err
	}
	terminal := proto.Clone(pending).(*action_servicepb.ActionRequest)
	terminal.State = action_servicepb.ActionState_ACTION_STATE_SUCCEEDED
	return stream.Send(&action_servicepb.WatchActionRequestResponse{ActionRequest: terminal})
}

func TestSessionBindsClonedIdentityAndRecoversWithoutResubmitting(t *testing.T) {
	service := &actionsService{}
	client := newTestClient(t, service, time.Second)
	defer client.Close()

	identity := &identitypb.Identity{Name: "sampler-user", Uid: "identity-incarnation-1"}
	session, err := client.Bind(testSessionID, identity)
	if err != nil {
		t.Fatal(err)
	}
	identity.Name = "mutated-after-bind"

	spoofed, err := structpb.NewStruct(map[string]any{
		"identity":   map[string]any{"name": "argument-cannot-rebind"},
		"session_id": "018f4e32-6632-7e7f-83dd-7f90ccf84f70",
	})
	if err != nil {
		t.Fatal(err)
	}
	request := &action_servicepb.SubmitActionRequestRequest{
		IdempotencyKey: "fixed-key", Action: &action_servicepb.ActionIdentity{Group: "test-group", Name: "run"},
		Arguments: spoofed, Title: "run test action",
	}
	if _, err := session.Submit(metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer forbidden"), request); !errors.Is(err, ErrDelegationContext) {
		t.Fatalf("Submit accepted caller authorization metadata: %v", err)
	}
	if _, err := session.Catalog(metadata.AppendToOutgoingContext(context.Background(), auth.DelegationMetadataKey, "forged")); !errors.Is(err, ErrDelegationContext) {
		t.Fatalf("Catalog accepted caller delegation metadata: %v", err)
	}

	groups, err := session.Catalog(context.Background())
	if err != nil || len(groups) != 1 || groups[0].GetKey() != "test-group" {
		t.Fatalf("Catalog() = %v, %v", groups, err)
	}
	receipt, err := session.Submit(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.GetIdempotencyKey() != "fixed-key" || !proto.Equal(receipt.GetArguments(), spoofed) {
		t.Fatalf("Submit receipt = %v", receipt)
	}
	recovered, err := session.Recover(context.Background(), "fixed-key")
	if err != nil || recovered.GetIdempotencyKey() != "fixed-key" {
		t.Fatalf("Recover() = %v, %v", recovered, err)
	}

	service.mu.Lock()
	defer service.mu.Unlock()
	if service.submissions != 1 {
		t.Errorf("SubmitActionRequest calls = %d, want one", service.submissions)
	}
	if len(service.listKeys) != 1 || service.listKeys[0] != "fixed-key" {
		t.Errorf("recovery lookup keys = %v, want exactly fixed-key", service.listKeys)
	}
	if len(service.records) != 3 {
		t.Fatalf("authenticated Action RPC count = %d, want Catalog, Submit and Recover", len(service.records))
	}
	for i, record := range service.records {
		if record.peer != peertls.CentralSamplerURI {
			t.Errorf("RPC %d peer SVID = %q, want CentralSampler", i, record.peer)
		}
		delegation := record.envelope
		if got := delegation.GetIdentity().GetName(); got != "sampler-user" {
			t.Errorf("RPC %d delegated Identity name = %q", i, got)
		}
		if got := delegation.GetIdentity().GetUid(); got != "identity-incarnation-1" {
			t.Errorf("RPC %d delegated Identity UID = %q", i, got)
		}
		if got := delegation.GetSession().GetSessionId(); got != testSessionID {
			t.Errorf("RPC %d delegated Session = %q", i, got)
		}
	}
}

func TestAwaitRetriesWatchAndDoesNotApplyUnaryTimeout(t *testing.T) {
	service := &actionsService{
		watchRetry:   true,
		watchStarted: make(chan struct{}, 1),
		releaseWatch: make(chan struct{}),
	}
	client := newTestClient(t, service, 150*time.Millisecond)
	defer client.Close()
	session, err := client.Bind(testSessionID, &identitypb.Identity{Name: "sampler-user", Uid: "identity-incarnation-1"})
	if err != nil {
		t.Fatal(err)
	}

	type awaitResult struct {
		request *action_servicepb.ActionRequest
		err     error
	}
	awaited := make(chan awaitResult, 1)
	go func() {
		request, err := session.Await(context.Background(), "018f4e32-6632-7e7f-83dd-7f90ccf84f69")
		awaited <- awaitResult{request: request, err: err}
	}()
	select {
	case <-service.watchStarted:
	case <-time.After(3 * time.Second):
		t.Fatal("watch did not reconnect after the transient failure")
	}
	// The stream remains open after rpcTimeout while unary Catalog calls still complete on the connection.
	time.Sleep(2 * 150 * time.Millisecond)
	if _, err := session.Catalog(context.Background()); err != nil {
		t.Fatalf("Catalog during a pending watch: %v", err)
	}
	close(service.releaseWatch)
	select {
	case result := <-awaited:
		if result.err != nil || result.request.GetState() != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED {
			t.Fatalf("Await() = %v, %v", result.request, result.err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Await did not return after the terminal receipt")
	}
	service.mu.Lock()
	watchCalls := service.watchCalls
	service.mu.Unlock()
	if watchCalls != 2 {
		t.Fatalf("WatchActionRequest calls = %d, want transient attempt plus reconnect", watchCalls)
	}
}

func TestUnaryRPCUsesConfiguredTimeout(t *testing.T) {
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	service := &actionsService{catalogWait: release, catalogEntered: entered}
	client := newTestClient(t, service, 100*time.Millisecond)
	defer client.Close()
	session, err := client.Bind(testSessionID, &identitypb.Identity{Name: "sampler-user", Uid: "identity-incarnation-1"})
	if err != nil {
		t.Fatal(err)
	}
	_, err = session.Catalog(context.Background())
	if status.Code(err) != codes.DeadlineExceeded {
		t.Fatalf("Catalog error = %v, want DeadlineExceeded", err)
	}
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("Catalog did not reach the service")
	}
	close(release)
}

func TestDialRequiresCentralSamplerSVIDAndMutualTLS(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	other := ca.Issue(t, peertls.ActionsURI)
	if _, err := Dial(context.Background(), "127.0.0.1:1", other, time.Second); err == nil {
		t.Fatal("Dial accepted a non-Central-Sampler SVID")
	}
}

func newTestClient(t *testing.T, service *actionsService, rpcTimeout time.Duration) *Client {
	t.Helper()
	ca := peertlstesting.NewCA(t)
	serverTLS := ca.Issue(t, peertls.ActionsURI)
	serverCredentials, err := (peertls.Security{Mode: peertls.MutualTLS, TLS: &serverTLS}).ServerCredentials(peertls.CentralSamplerURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(serverCredentials))
	action_servicepb.RegisterActionServiceServer(server, service)
	target := grpcserver.Serve(t, server)
	clientTLS := ca.Issue(t, peertls.CentralSamplerURI)
	client, err := Dial(context.Background(), target, clientTLS, rpcTimeout)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return client
}
