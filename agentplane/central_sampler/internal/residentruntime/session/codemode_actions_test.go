package session

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"sync"
	"testing"
	"time"

	runtimetesting "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/residentruntime/testing"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/actions"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
)

const actionFixtureRequestID = "018f4e32-6632-7e7f-83dd-7f90ccf84f69"

type codeModeActionService struct {
	pb.UnimplementedActionServiceServer

	mu                 sync.Mutex
	requests           map[string]*pb.ActionRequest
	submissions        []*pb.SubmitActionRequestRequest
	recoveryKeys       []string
	delegations        []*delegationpb.Delegation
	loseSubmitResponse bool
	terminalState      pb.ActionState
	watchStarted       chan struct{}
	releaseWatch       chan struct{}
}

func (s *codeModeActionService) recordPeer(ctx context.Context) error {
	identity, err := peertls.PeerIdentity(ctx)
	if err != nil {
		return err
	}
	if identity != peertls.CentralSamplerURI {
		return status.Errorf(codes.Unauthenticated, "unexpected peer SVID %q", identity)
	}
	md, _ := metadata.FromIncomingContext(ctx)
	if len(md.Get("authorization")) != 0 {
		return status.Error(codes.Unauthenticated, "unexpected authorization metadata")
	}
	values := md.Get(auth.DelegationMetadataKey)
	if len(values) != 1 {
		return status.Errorf(codes.Unauthenticated, "got %d delegation envelopes", len(values))
	}
	envelope := new(delegationpb.Delegation)
	if err := proto.Unmarshal([]byte(values[0]), envelope); err != nil {
		return status.Error(codes.Unauthenticated, "invalid delegation envelope")
	}
	s.mu.Lock()
	s.delegations = append(s.delegations, proto.Clone(envelope).(*delegationpb.Delegation))
	s.mu.Unlock()
	return nil
}

func (s *codeModeActionService) ListActionGroups(ctx context.Context, _ *pb.ListActionGroupsRequest) (*pb.ListActionGroupsResponse, error) {
	if err := s.recordPeer(ctx); err != nil {
		return nil, err
	}
	schema, err := structpb.NewStruct(map[string]any{
		"type":       "object",
		"properties": map[string]any{"value": map[string]any{"type": "string"}},
	})
	if err != nil {
		return nil, err
	}
	return &pb.ListActionGroupsResponse{ActionGroups: []*pb.ActionGroup{{
		Key: "fixture", ExecutorKind: pb.ExecutorKind_EXECUTOR_KIND_POD, Available: true,
		Actions: []*pb.Action{{Group: "fixture", Name: "echo", Description: "Fixture Action", InputSchema: schema}},
	}}}, nil
}

func (s *codeModeActionService) SubmitActionRequest(ctx context.Context, request *pb.SubmitActionRequestRequest) (*pb.SubmitActionRequestResponse, error) {
	if err := s.recordPeer(ctx); err != nil {
		return nil, err
	}
	copyRequest := proto.Clone(request).(*pb.SubmitActionRequestRequest)
	receipt := &pb.ActionRequest{
		Id: actionFixtureRequestID, IdempotencyKey: request.GetIdempotencyKey(),
		Action:    proto.Clone(request.GetAction()).(*pb.ActionIdentity),
		Arguments: proto.Clone(request.GetArguments()).(*structpb.Struct),
		Title:     request.GetTitle(), State: pb.ActionState_ACTION_STATE_DECISION_PENDING,
	}
	s.mu.Lock()
	s.submissions = append(s.submissions, copyRequest)
	s.requests[receipt.GetIdempotencyKey()] = proto.Clone(receipt).(*pb.ActionRequest)
	loseResponse := s.loseSubmitResponse
	s.mu.Unlock()
	if loseResponse {
		return nil, status.Error(codes.Unavailable, "Submit response lost after admission")
	}
	return &pb.SubmitActionRequestResponse{ActionRequest: receipt}, nil
}

func (s *codeModeActionService) ListActionRequests(ctx context.Context, request *pb.ListActionRequestsRequest) (*pb.ListActionRequestsResponse, error) {
	if err := s.recordPeer(ctx); err != nil {
		return nil, err
	}
	key := request.GetIdempotencyKey()
	s.mu.Lock()
	s.recoveryKeys = append(s.recoveryKeys, key)
	receipt := s.requests[key]
	if receipt != nil {
		receipt = proto.Clone(receipt).(*pb.ActionRequest)
	}
	s.mu.Unlock()
	if receipt == nil {
		return &pb.ListActionRequestsResponse{}, nil
	}
	return &pb.ListActionRequestsResponse{ActionRequests: []*pb.ActionRequest{receipt}}, nil
}

func (s *codeModeActionService) WatchActionRequest(request *pb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[pb.WatchActionRequestResponse]) error {
	if err := s.recordPeer(stream.Context()); err != nil {
		return err
	}
	s.mu.Lock()
	var receipt *pb.ActionRequest
	for _, submitted := range s.requests {
		if submitted.GetId() == request.GetRequestId() {
			receipt = proto.Clone(submitted).(*pb.ActionRequest)
			break
		}
	}
	terminalState := s.terminalState
	watchStarted := s.watchStarted
	releaseWatch := s.releaseWatch
	s.mu.Unlock()
	if receipt == nil {
		return status.Error(codes.NotFound, "request not found")
	}
	if err := stream.Send(&pb.WatchActionRequestResponse{ActionRequest: receipt}); err != nil {
		return err
	}
	if watchStarted != nil {
		select {
		case watchStarted <- struct{}{}:
		default:
		}
	}
	if releaseWatch != nil {
		select {
		case <-releaseWatch:
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}
	receipt.State = terminalState
	switch terminalState {
	case pb.ActionState_ACTION_STATE_SUCCEEDED:
		result, err := structpb.NewStruct(map[string]any{"answer": "fixture result"})
		if err != nil {
			return err
		}
		receipt.Execution = &pb.Execution{
			Id: uuid.NewString(), State: pb.ExecutionState_EXECUTION_STATE_SUCCEEDED,
			Result: structpb.NewStructValue(result),
		}
	case pb.ActionState_ACTION_STATE_FAILED:
		receipt.Execution = &pb.Execution{
			Id: uuid.NewString(), State: pb.ExecutionState_EXECUTION_STATE_FAILED,
			Error: &pb.ExecutionError{Kind: pb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INTERNAL, Message: "fixture failure"},
		}
	default:
		return fmt.Errorf("unsupported fixture terminal Action state %s", terminalState)
	}
	return stream.Send(&pb.WatchActionRequestResponse{ActionRequest: receipt})
}

func newCodeModeActionService(t *testing.T, terminal pb.ActionState, loseSubmitResponse bool, waitForWatch bool) (*codeModeActionService, *actions.Client) {
	t.Helper()
	service := &codeModeActionService{
		requests: make(map[string]*pb.ActionRequest), loseSubmitResponse: loseSubmitResponse,
		terminalState: terminal,
	}
	if waitForWatch {
		service.watchStarted = make(chan struct{}, 1)
		service.releaseWatch = make(chan struct{})
	}
	ca := peertlstesting.NewCA(t)
	serverTLS := ca.Issue(t, peertls.ActionsURI)
	serverCredentials, err := (peertls.Security{Mode: peertls.MutualTLS, TLS: &serverTLS}).ServerCredentials(peertls.CentralSamplerURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(serverCredentials))
	pb.RegisterActionServiceServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	clientTLS := ca.Issue(t, peertls.CentralSamplerURI)
	client, err := actions.Dial(t.Context(), listener.Addr().String(), clientTLS, 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return service, client
}

func TestCodeModeActionsRunWithoutExecutorAndPersistTerminalReceipts(t *testing.T) {
	for _, terminal := range []pb.ActionState{
		pb.ActionState_ACTION_STATE_SUCCEEDED,
		pb.ActionState_ACTION_STATE_FAILED,
	} {
		t.Run(terminal.String(), func(t *testing.T) {
			store, owner := claimedExecutorStore(t)
			session, err := store.GetSessionForConversation(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			startTurnForStore(t, store, session, owner, "action-turn")
			service, client := newCodeModeActionService(t, terminal, false, false)
			manager, peers := newFakeCodeModeManager(t)
			manager.store, manager.actions = store, client
			code, err := manager.OpenSession(t.Context(), session.ConversationID, owner, nil)
			if err != nil {
				t.Fatal(err)
			}
			if code.executor != nil {
				t.Fatal("Action-capable Code Mode unexpectedly has an attached environment executor")
			}
			peer := peers()[0]
			if err := peer.Send(actionDelegateFrame(1, code.ID())); err != nil {
				t.Fatal(err)
			}
			response := receivePeerFrame(t, peer)
			assertActionDelegateOutcome(t, response, 1, terminal)
			assertActionReceiptEvents(t, store, session.ID, terminal)
			runtimetesting.AssertNoUnresolvedNestedIntents(t, store, owner)
			assertActionAttribution(t, service, session)

			service.mu.Lock()
			submissions := len(service.submissions)
			peerCount := len(service.delegations)
			service.mu.Unlock()
			if submissions != 1 {
				t.Fatalf("Action submissions=%d, want one", submissions)
			}
			if peerCount < 3 {
				t.Fatalf("authenticated Action RPCs=%d, want Catalog, Submit, and Watch", peerCount)
			}
		})
	}
}

func TestCodeModePendingActionWatchDoesNotBlockCellNotification(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	session, err := store.GetSessionForConversation(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	startTurnForStore(t, store, session, owner, "pending-action-turn")
	service, client := newCodeModeActionService(t, pb.ActionState_ACTION_STATE_SUCCEEDED, false, true)
	manager, peers := newFakeCodeModeManager(t)
	manager.store, manager.actions = store, client
	code, err := manager.OpenSession(t.Context(), session.ConversationID, owner, nil)
	if err != nil {
		t.Fatal(err)
	}
	peer := peers()[0]
	if err := peer.Send(actionDelegateFrame(10, code.ID())); err != nil {
		t.Fatal(err)
	}
	select {
	case <-service.watchStarted:
	case <-time.After(3 * time.Second):
		t.Fatal("Action watch did not reach its pending state")
	}
	if err := peer.SendNotification(11, code.ID(), "unrelated-cell", "still running"); err != nil {
		t.Fatal(err)
	}
	runtimetesting.AssertDelegateResponse(t, receivePeerFrame(t, peer), 11, "ok")
	if count := queuedNotifications(t, store, session.ConversationID); count != 1 {
		t.Fatalf("pending Action prevented durable cell notification: queued=%d", count)
	}
	close(service.releaseWatch)
	assertActionDelegateOutcome(t, receivePeerFrame(t, peer), 10, pb.ActionState_ACTION_STATE_SUCCEEDED)
	assertActionAttribution(t, service, session)
	runtimetesting.AssertNoUnresolvedNestedIntents(t, store, owner)
}

func TestCodeModeUncertainSubmitRecoversExactIdempotencyKey(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	session, err := store.GetSessionForConversation(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	startTurnForStore(t, store, session, owner, "uncertain-action-turn")
	service, client := newCodeModeActionService(t, pb.ActionState_ACTION_STATE_SUCCEEDED, true, false)
	manager, peers := newFakeCodeModeManager(t)
	manager.store, manager.actions = store, client
	code, err := manager.OpenSession(t.Context(), session.ConversationID, owner, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := peers()[0].Send(actionDelegateFrame(20, code.ID())); err != nil {
		t.Fatal(err)
	}
	assertActionDelegateOutcome(t, receivePeerFrame(t, peers()[0]), 20, pb.ActionState_ACTION_STATE_SUCCEEDED)
	assertActionAttribution(t, service, session)
	service.mu.Lock()
	defer service.mu.Unlock()
	if len(service.submissions) != 1 {
		t.Fatalf("Submit RPC count=%d, want one after uncertain response", len(service.submissions))
	}
	submitKey := service.submissions[0].GetIdempotencyKey()
	if len(service.recoveryKeys) != 1 || service.recoveryKeys[0] != submitKey {
		t.Fatalf("recovery keys=%q, want exactly Submit key %q", service.recoveryKeys, submitKey)
	}
	if submitKey == "" {
		t.Fatal("uncertain Submit used an empty idempotency key")
	}
	runtimetesting.AssertNoUnresolvedNestedIntents(t, store, owner)
}

func actionDelegateFrame(id int64, sessionID string) codemode.Frame {
	namespace := "fixture"
	return codemode.Frame{
		Type: codemode.DelegateRequest, ID: &id, SessionID: sessionID,
		Request: &codemode.Delegate{
			Type: "tool/invoke", CallID: "action-call", CellID: "action-cell",
			Invocation: codemode.ToolInvocation{
				RuntimeToolCallID: "runtime-action-call", ToolName: codemode.ToolName{Name: "echo", Namespace: &namespace},
				ToolKind: "function", Input: json.RawMessage(`{"value":"fixture input"}`),
			},
		},
	}
}

func assertActionDelegateOutcome(t *testing.T, fields map[string]json.RawMessage, wantID int64, state pb.ActionState) {
	t.Helper()
	var response codemode.DelegateResponse
	if err := json.Unmarshal(runtimetesting.MarshalFields(t, fields), &response); err != nil {
		t.Fatal(err)
	}
	if response.Type != "delegate/response" || response.ID != wantID || response.Result.Status != "ok" ||
		response.Result.Value == nil || response.Result.Value.Type != "tool/result" {
		t.Fatalf("Action delegate response=%+v, want a tool/result for id=%d", response, wantID)
	}
	var result struct {
		IsError           bool `json:"isError"`
		StructuredContent struct {
			Answer string `json:"answer"`
			State  string `json:"state"`
		} `json:"structuredContent"`
	}
	if err := json.Unmarshal(response.Result.Value.Result, &result); err != nil {
		t.Fatalf("decode Action tool result %s: %v", response.Result.Value.Result, err)
	}
	switch state {
	case pb.ActionState_ACTION_STATE_SUCCEEDED:
		if result.IsError || result.StructuredContent.Answer != "fixture result" {
			t.Fatalf("successful Action produced tool result %+v", result)
		}
	case pb.ActionState_ACTION_STATE_FAILED:
		if !result.IsError || result.StructuredContent.State != "failed" {
			t.Fatalf("failed Action did not produce a typed tool error: %+v", result)
		}
	default:
		t.Fatalf("unsupported expected Action state %s", state)
	}
}

func assertActionAttribution(t *testing.T, service *codeModeActionService, session *sessionstore.Session) {
	t.Helper()
	wantIdentity, err := session.ExecutionIdentity()
	if err != nil {
		t.Fatal(err)
	}
	service.mu.Lock()
	delegations := append([]*delegationpb.Delegation(nil), service.delegations...)
	service.mu.Unlock()
	if len(delegations) == 0 {
		t.Fatal("Action Service received no delegated RPCs")
	}
	for index, envelope := range delegations {
		if !proto.Equal(envelope.GetIdentity(), wantIdentity) {
			t.Errorf("Action RPC %d delegated identity=%v, want retained Session identity %v", index, envelope.GetIdentity(), wantIdentity)
		}
		if got := envelope.GetSession().GetSessionId(); got != session.ID {
			t.Errorf("Action RPC %d provenance Session=%q, want retained Session %q", index, got, session.ID)
		}
	}
}

func assertActionReceiptEvents(t *testing.T, store *sessionstore.Store, sessionID string, terminal pb.ActionState) {
	t.Helper()
	entries, err := store.Events(t.Context(), sessionID, 0, 4096)
	if err != nil {
		t.Fatal(err)
	}
	var receipts []*pb.ActionRequest
	for _, entry := range entries {
		boundary := entry.GetEvent().GetBoundaryRecord()
		if boundary == nil || boundary.GetBoundary() != "tool" || boundary.GetDirection() != "dispatch" {
			continue
		}
		var evidence struct {
			ActionReceipt json.RawMessage `json:"action_receipt"`
		}
		if err := json.Unmarshal(boundary.GetPayload(), &evidence); err != nil {
			t.Fatal(err)
		}
		if len(evidence.ActionReceipt) == 0 {
			continue
		}
		receipt := new(pb.ActionRequest)
		if err := protojson.Unmarshal(evidence.ActionReceipt, receipt); err != nil {
			t.Fatal(err)
		}
		receipts = append(receipts, receipt)
	}
	if len(receipts) < 2 {
		t.Fatalf("durable Action receipt evidence count=%d, want initial and terminal receipts", len(receipts))
	}
	key := receipts[0].GetIdempotencyKey()
	if key == "" {
		t.Fatal("durable receipt omitted its idempotency key")
	}
	foundTerminal := false
	for _, receipt := range receipts {
		if receipt.GetIdempotencyKey() != key || receipt.GetId() != actionFixtureRequestID {
			t.Fatalf("receipt evidence changed identity: key=%q id=%q", receipt.GetIdempotencyKey(), receipt.GetId())
		}
		if receipt.GetState() == terminal {
			foundTerminal = true
		}
	}
	if !foundTerminal {
		t.Fatalf("durable Action receipts never recorded terminal state %s", terminal)
	}
}
