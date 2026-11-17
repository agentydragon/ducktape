package rpc

import (
	"context"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// HarnessHost shares the Sampler's admission, immutable creation and command fencing.
// Sampler remains registered until its callers have migrated.
type HarnessHost struct {
	harnesshostpb.UnimplementedHarnessHostServer
	Sampler *Sampler
}

func (h *HarnessHost) CreateSession(ctx context.Context, r *harnesshostpb.CreateSessionRequest) (*harnesshostpb.CreateSessionResponse, error) {
	response, err := h.Sampler.CreateSession(ctx, &samplerpb.CreateSessionRequest{SessionId: r.GetSessionId(), Spec: r.GetSpec(), SetupScript: r.SetupScript, ExecutionIdentity: r.GetExecutionIdentity()})
	if err != nil {
		return nil, err
	}
	return &harnesshostpb.CreateSessionResponse{Attached: response.GetAttached()}, nil
}

func (h *HarnessHost) SubmitInput(ctx context.Context, r *harnesshostpb.SubmitInputRequest) (*harnesshostpb.SubmitInputResponse, error) {
	response, err := h.Sampler.SubmitInput(ctx, &environmentruntimepb.SubmitInputRequest{SessionId: r.GetSessionId(), CommandId: r.GetCommandId(), Text: r.GetText()})
	if err != nil {
		return nil, err
	}
	return &harnesshostpb.SubmitInputResponse{Admission: response.GetAdmission()}, nil
}

func (h *HarnessHost) ChangeModel(ctx context.Context, r *harnesshostpb.ChangeModelRequest) (*harnesshostpb.ChangeModelResponse, error) {
	response, err := h.Sampler.ChangeModel(ctx, &environmentruntimepb.ChangeModelRequest{SessionId: r.GetSessionId(), CommandId: r.GetCommandId(), Model: r.GetModel()})
	if err != nil {
		return nil, err
	}
	return &harnesshostpb.ChangeModelResponse{Admission: response.GetAdmission()}, nil
}

func (h *HarnessHost) ChangeReasoningEffort(ctx context.Context, r *harnesshostpb.ChangeReasoningEffortRequest) (*harnesshostpb.ChangeReasoningEffortResponse, error) {
	response, err := h.Sampler.ChangeReasoningEffort(ctx, &environmentruntimepb.ChangeReasoningEffortRequest{SessionId: r.GetSessionId(), CommandId: r.GetCommandId(), Effort: r.GetEffort()})
	if err != nil {
		return nil, err
	}
	return &harnesshostpb.ChangeReasoningEffortResponse{Admission: response.GetAdmission()}, nil
}

func (h *HarnessHost) InterruptTurn(ctx context.Context, r *harnesshostpb.InterruptTurnRequest) (*harnesshostpb.InterruptTurnResponse, error) {
	response, err := h.Sampler.InterruptTurn(ctx, &environmentruntimepb.InterruptTurnRequest{SessionId: r.GetSessionId(), CommandId: r.GetCommandId(), TurnId: r.GetTurnId()})
	if err != nil {
		return nil, err
	}
	return &harnesshostpb.InterruptTurnResponse{Admission: response.GetAdmission()}, nil
}

func (h *HarnessHost) ListSessions(ctx context.Context, _ *harnesshostpb.ListSessionsRequest) (*harnesshostpb.ListSessionsResponse, error) {
	ctx, cancel, principal, err := h.Sampler.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	sessions, err := h.Sampler.Store.HostSessions(ctx, principal)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	return &harnesshostpb.ListSessionsResponse{Sessions: sessions}, nil
}

func (h *HarnessHost) WatchSessions(_ *harnesshostpb.WatchSessionsRequest, stream grpc.ServerStreamingServer[harnesshostpb.WatchSessionsResponse]) error {
	s := h.Sampler
	_, cancelAdmission, _, admissionErr := s.admitted(stream.Context())
	cancelAdmission()
	if admissionErr != nil {
		return admissionErr
	}
	changed, unsubscribe, err := s.Wakeups.Subscribe(sessionstore.OutboxChannel, sessionstore.WorkChannel)
	if err != nil {
		return samplerError(stream.Context(), err)
	}
	defer unsubscribe()
	send := func(message *harnesshostpb.WatchSessionsResponse) error {
		return streaming.SendWithin(stream.Context(), time.Duration(s.Settings.RPCTimeoutSeconds)*time.Second, stream.Send, message)
	}
	previous := map[string]*harnesshostpb.Session{}
	for {
		// Subscribe before reading, so a commit racing the snapshot leaves a wakeup.
		// Recheck service admission for every batch; do not bind the stream's lifetime
		// to the unary RPC timeout or poll the database while it is idle.
		if !s.Wakeups.Connected() {
			return status.Error(codes.Unavailable, "Session status wakeups disconnected")
		}
		generation := s.Wakeups.Generation()
		ctx, cancel, principal, err := s.admitted(stream.Context())
		if err != nil {
			cancel()
			return err
		}
		sessions, err := s.Store.HostSessions(ctx, principal)
		mapped := samplerError(ctx, err)
		cancel()
		if err != nil {
			return mapped
		}
		if !s.Wakeups.Connected() || s.Wakeups.Generation() != generation {
			return status.Error(codes.Unavailable, "Session status wakeups changed; reconnect for a fresh snapshot")
		}
		current := make(map[string]*harnesshostpb.Session, len(sessions))
		for _, session := range sessions {
			current[session.SessionId] = session
		}
		for id := range previous {
			if current[id] == nil {
				return status.Error(codes.Unavailable, "Session removed; reconnect for a fresh snapshot")
			}
		}
		for _, session := range sessions {
			if !proto.Equal(previous[session.SessionId], session) {
				if err := send(&harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_Upsert{Upsert: session}}); err != nil {
					return err
				}
			}
		}
		if err := send(&harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_CaughtUp{CaughtUp: &harnesshostpb.CaughtUp{}}}); err != nil {
			return err
		}
		previous = current
		if err := h.waitForSessionsChange(stream.Context(), changed); err != nil {
			return err
		}
	}
}

// Revalidate idle streams without polling Session rows. Only a Postgres wakeup
// permits another status read.
func (h *HarnessHost) waitForSessionsChange(ctx context.Context, changed <-chan struct{}) error {
	timer := time.NewTicker(30 * time.Second)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-changed:
			return nil
		case <-timer.C:
			_, cancel, _, err := h.Sampler.admitted(ctx)
			cancel()
			if err != nil {
				return err
			}
		}
	}
}
