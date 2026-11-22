package dispatcher

import (
	"context"
	"encoding/json"
	"strings"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/resumetoken"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

func validNotice(n *dispatcherpb.CommandNoticeRevision) bool {
	return n.GetCommandId() != "" && !strings.ContainsRune(n.GetCommandId(), 0) && n.GetReceiptVersion() > 0
}

func (s *Service) AcknowledgeCommandNotice(ctx context.Context, request *dispatcherpb.AcknowledgeCommandNoticeRequest) (*emptypb.Empty, error) {
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	component, ok := caller.(store.ServiceCaller)
	if !ok || !s.NoticeWriters.Has(component.SPIFFEID) {
		return nil, status.Error(codes.PermissionDenied, "acknowledging operator attention requires an authorized operator service")
	}

	if err := required(request.GetSessionId()); err != nil {
		return nil, err
	}
	if !validNotice(request.GetNotice()) {
		return nil, status.Error(codes.InvalidArgument, "expected a command ID and positive receipt version")
	}
	if err := s.Store.AcknowledgeCommandNotice(ctx, request.GetSessionId(), caller, request.GetNotice()); err != nil {
		return nil, apiError(err)
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) BatchGetCommandNoticeAcknowledgements(ctx context.Context, request *dispatcherpb.BatchGetCommandNoticeAcknowledgementsRequest) (*dispatcherpb.CommandNoticeAcknowledgements, error) {
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := required(request.GetSessionId()); err != nil {
		return nil, err
	}
	notices := request.GetNotices()
	if len(notices) == 0 || len(notices) > 100 {
		return nil, status.Error(codes.InvalidArgument, "expected 1 to 100 notice revisions")
	}
	for _, n := range notices {
		if !validNotice(n) {
			return nil, status.Error(codes.InvalidArgument, "expected a command ID and positive receipt version")
		}
	}
	result, _, err := s.Store.NoticeAcknowledgements(ctx, request.GetSessionId(), caller, notices, 0, len(notices))
	if err != nil {
		return nil, apiError(err)
	}
	return result, nil
}

func noticePageDigest(id string, caller store.Caller) [32]byte {
	b, _ := json.Marshal(struct {
		Resource, Session string
		Scope             [32]byte
	}{"notice-acknowledgements-v1", id, sessionWatchDigest(caller)})
	return resumetoken.Digest(b)
}

func (s *Service) ListCommandNoticeAcknowledgements(ctx context.Context, request *dispatcherpb.ListCommandNoticeAcknowledgementsRequest) (*dispatcherpb.CommandNoticeAcknowledgements, error) {
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	id := request.GetSessionId()
	if err := required(id); err != nil {
		return nil, err
	}
	size := request.GetPageSize()
	if size > 100 {
		return nil, status.Error(codes.InvalidArgument, "page size exceeds 100")
	}
	if size == 0 {
		size = 25
	}
	var before int64
	scope := noticePageDigest(id, caller)
	if token := request.GetPageToken(); token != "" {
		before, err = resumetoken.Decode(token, scope)
		if err != nil || before <= 0 {
			return nil, status.Error(codes.InvalidArgument, "invalid notice history page token")
		}
	}
	result, next, err := s.Store.NoticeAcknowledgements(ctx, id, caller, nil, before, int(size))
	if err != nil {
		return nil, apiError(err)
	}
	if next != 0 {
		token := resumetoken.Encode(next, scope)
		result.NextPageToken = &token
	}
	return result, nil
}
