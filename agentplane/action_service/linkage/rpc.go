package linkage

import (
	"bytes"
	"context"
	"errors"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Service presents the public OAuth callback over gRPC. The state is the capability authorizing a flow.
type Service struct {
	linkage_servicepb.UnimplementedLinkageServiceServer
	authority *Authority
}

func NewService(authority *Authority) *Service { return &Service{authority: authority} }

func (s *Service) GetClientMetadata(_ context.Context, _ *linkage_servicepb.GetClientMetadataRequest) (*linkage_servicepb.GetClientMetadataResponse, error) {
	if s.authority.document == nil {
		return nil, status.Error(codes.NotFound, "no shared client metadata document")
	}
	return &linkage_servicepb.GetClientMetadataResponse{JsonDocument: bytes.Clone(s.authority.document)}, nil
}

func (s *Service) CompleteCallback(ctx context.Context, request *linkage_servicepb.CompleteCallbackRequest) (*linkage_servicepb.CompleteCallbackResponse, error) {
	if request.GetState() == "" || request.GetCode() == "" {
		return nil, status.Error(codes.InvalidArgument, "OAuth callback is missing state or code")
	}
	linked, err := s.authority.Callback(ctx, request.GetState(), request.GetCode())
	var refused *tokenError
	switch {
	case errors.Is(err, ErrInvalidFlow):
		return nil, status.Error(codes.FailedPrecondition, ErrInvalidFlow.Error())
	case errors.Is(err, ErrUnknownServer):
		return nil, status.Error(codes.NotFound, ErrUnknownServer.Error())
	case errors.As(err, &refused):
		// Provider error descriptions can contain attacker-controlled text. Keep the gRPC status safe;
		// callers identify the token-exchange case through the typed ErrorInfo detail.
		const safeMessage = "MCP authorization code exchange failed"
		failure, detailErr := status.New(codes.Unavailable, safeMessage).WithDetails(&errdetails.ErrorInfo{
			Reason: "MCP_TOKEN_EXCHANGE_FAILED", Domain: "agentplane.actions",
		})
		if detailErr != nil {
			return nil, status.Error(codes.Unavailable, safeMessage)
		}
		return nil, failure.Err()
	case err != nil:
		slog.WarnContext(ctx, "MCP linkage callback failed", "error", err)
		return nil, status.Error(codes.Unavailable, "the Action Service is temporarily unavailable")
	}
	return &linkage_servicepb.CompleteCallbackResponse{Linkage: linked}, nil
}
