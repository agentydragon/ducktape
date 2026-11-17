package rpc

import (
	"context"
	"time"

	sampleradminpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/sampleradminpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"google.golang.org/grpc"
)

// SamplerAdmin exposes diagnostic and recovery operations under a separate service grant.
type SamplerAdmin struct {
	sampleradminpb.UnimplementedSamplerAdminServer
	Sampler
}

func (s *SamplerAdmin) admitted(ctx context.Context) (context.Context, context.CancelFunc, string, error) {
	return admitted(ctx, s.Settings.RPCTimeoutSeconds, s.Settings.AdminServiceCallers)
}

func (s *SamplerAdmin) Compact(ctx context.Context, r *sampleradminpb.CompactRequest) (*sampleradminpb.CompactResponse, error) {
	entry, err := s.Sampler.commandUsing(ctx, r.GetSessionId(), &commandpb.Command{
		CommandId: r.GetCommandId(), Operation: &commandpb.Command_Compact{Compact: &commandpb.Compact{}},
	}, s.admitted)
	return &sampleradminpb.CompactResponse{Admission: entry}, err
}

func adminSessionDescription(session *sessionstore.Session) (*sampleradminpb.GetSessionResponse, error) {
	description, err := sessionstore.Describe(session)
	if err != nil {
		return nil, err
	}
	return &sampleradminpb.GetSessionResponse{
		Attached: description.GetAttached(), Executor: description.GetExecutor(),
		ExpectedIncarnationUid: description.GetExpectedIncarnationUid(), Blocked: description.GetBlocked(),
		BlockedReason: description.GetBlockedReason(),
	}, nil
}

func (s *SamplerAdmin) RecoverSession(ctx context.Context, r *sampleradminpb.RecoverSessionRequest) (*sampleradminpb.GetSessionResponse, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	session, err := s.Store.RecoverSession(ctx, r.GetSessionId(), principal, r.GetExpectedCursor(), r.GetOperatorNote(), r.GetReplacementIncarnationUid())
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	description, err := adminSessionDescription(session)
	return description, samplerError(ctx, err)
}

func (s *SamplerAdmin) GetSession(ctx context.Context, r *sampleradminpb.GetSessionRequest) (*sampleradminpb.GetSessionResponse, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	session, err := s.Store.GetSession(ctx, r.GetSessionId(), principal)
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	description, err := adminSessionDescription(session)
	return description, samplerError(ctx, err)
}

func (s *SamplerAdmin) GetCommand(ctx context.Context, r *sampleradminpb.GetCommandRequest) (*sampleradminpb.GetCommandResponse, error) {
	ctx, cancel, principal, err := s.admitted(ctx)
	defer cancel()
	if err != nil {
		return nil, err
	}
	if _, err := s.Store.GetSession(ctx, r.GetSessionId(), principal); err != nil {
		return nil, samplerError(ctx, err)
	}
	result, err := s.Store.GetCommand(ctx, r.GetSessionId(), r.GetCommandId())
	if err != nil {
		return nil, samplerError(ctx, err)
	}
	return &sampleradminpb.GetCommandResponse{
		Admission: result.GetAdmission(), Outcome: result.GetOutcome(), FailureReason: result.GetFailureReason(),
	}, nil
}

func (s *SamplerAdmin) ReadProviderRequest(r *sampleradminpb.ReadProviderRequestRequest, stream grpc.ServerStreamingServer[sampleradminpb.ReadProviderRequestResponse]) error {
	ctx, cancel, principal, err := s.admitted(stream.Context())
	if err != nil {
		cancel()
		return err
	}
	defer cancel()

	request, err := s.Store.LoadProviderRequest(ctx, r.GetSessionId(), principal, r.GetRequestId())
	if err != nil {
		return samplerError(ctx, err)
	}
	metadata := &sampleradminpb.ReadProviderRequestMetadata{
		RequestId: request.RequestID,
		Method:    request.Method, Url: request.URL, Headers: sessionstore.ProviderRequestHeadersWithoutAuthentication(request.Headers),
	}
	if err := stream.Send(&sampleradminpb.ReadProviderRequestResponse{Item: &sampleradminpb.ReadProviderRequestResponse_Metadata{Metadata: metadata}}); err != nil {
		return err
	}
	for offset := 0; offset < len(request.Body); offset += maxProviderRequestBodyChunkSize {
		if ctx.Err() != nil {
			return samplerError(ctx, ctx.Err())
		}
		end := min(offset+maxProviderRequestBodyChunkSize, len(request.Body))
		chunk := request.Body[offset:end]
		if err := stream.Send(&sampleradminpb.ReadProviderRequestResponse{Item: &sampleradminpb.ReadProviderRequestResponse_BodyChunk{BodyChunk: chunk}}); err != nil {
			return err
		}
	}
	return nil
}

func (s *SamplerAdmin) FollowEvents(r *sampleradminpb.FollowEventsRequest, stream grpc.ServerStreamingServer[sampleradminpb.FollowEventsResponse]) error {
	changed, unsubscribe, err := s.Wakeups.Subscribe(sessionstore.OutboxChannel)
	if err != nil {
		return samplerError(stream.Context(), err)
	}
	defer unsubscribe()
	cursor := r.GetAfterCursor()
	for {
		ctx, cancel, principal, err := s.admitted(stream.Context())
		if err != nil {
			cancel()
			return err
		}
		if _, err = s.Store.GetSession(ctx, r.GetSessionId(), principal); err != nil {
			mapped := samplerError(ctx, err)
			cancel()
			return mapped
		}
		entries, err := s.Store.Events(ctx, r.GetSessionId(), cursor, 256)
		mapped := samplerError(ctx, err)
		cancel()
		if err != nil {
			return mapped
		}
		for _, entry := range entries {
			if err := stream.Send(&sampleradminpb.FollowEventsResponse{Entry: entry}); err != nil {
				return err
			}
			cursor = entry.GetCursor()
		}
		if len(entries) > 0 {
			continue
		}
		if !r.GetFollow() {
			return nil
		}
		// Recheck the TLS peer certificate periodically, even without journal traffic.
		timer := time.NewTimer(30 * time.Second)
		select {
		case <-stream.Context().Done():
			timer.Stop()
			return samplerError(stream.Context(), stream.Context().Err())
		case <-changed:
			timer.Stop()
		case <-timer.C:
		}
	}
}

var _ sampleradminpb.SamplerAdminServer = (*SamplerAdmin)(nil)
