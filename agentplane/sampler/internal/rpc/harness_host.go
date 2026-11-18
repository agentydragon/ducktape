package rpc

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// HarnessHost exposes the sampler's admission, immutable creation and command fencing.
type HarnessHost struct {
	harnesshostpb.UnimplementedHarnessHostServer
	Sampler *Sampler
}

func (h *HarnessHost) CreateSession(ctx context.Context, r *harnesshostpb.CreateSessionRequest) (*harnesshostpb.CreateSessionResponse, error) {
	return h.Sampler.CreateSession(ctx, r)
}

func (h *HarnessHost) SubmitInput(ctx context.Context, r *harnesshostpb.SubmitInputRequest) (*harnesshostpb.SubmitInputResponse, error) {
	return h.Sampler.SubmitInput(ctx, r)
}

func (h *HarnessHost) ChangeModel(ctx context.Context, r *harnesshostpb.ChangeModelRequest) (*harnesshostpb.ChangeModelResponse, error) {
	return h.Sampler.ChangeModel(ctx, r)
}

func (h *HarnessHost) ChangeReasoningEffort(ctx context.Context, r *harnesshostpb.ChangeReasoningEffortRequest) (*harnesshostpb.ChangeReasoningEffortResponse, error) {
	return h.Sampler.ChangeReasoningEffort(ctx, r)
}

func (h *HarnessHost) InterruptTurn(ctx context.Context, r *harnesshostpb.InterruptTurnRequest) (*harnesshostpb.InterruptTurnResponse, error) {
	return h.Sampler.InterruptTurn(ctx, r)
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
	if err := grpc.SetHeader(ctx, metadata.Pairs(harnesshost.SessionVersionMetadataKey, harnesshost.SessionVersion)); err != nil {
		return nil, err
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
	if err := stream.SendHeader(metadata.Pairs(harnesshost.SessionVersionMetadataKey, harnesshost.SessionVersion)); err != nil {
		return err
	}
	send := func(message *harnesshostpb.WatchSessionsResponse) error {
		return streaming.SendWithin(stream.Context(), time.Duration(s.Settings.RPCTimeoutSeconds)*time.Second, stream.Send, message)
	}
	previous := map[string]*harnesshostpb.Session{}
	generation := s.Wakeups.Generation()
	for {
		// Subscribe before reading, so a commit racing the snapshot leaves a wakeup.
		// Recheck service admission for every batch; do not bind the stream's lifetime
		// to the unary RPC timeout or poll the database while it is idle.
		if !s.Wakeups.Connected() || s.Wakeups.Generation() != generation {
			return status.Error(codes.Unavailable, "Session status wakeups disconnected")
		}
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
