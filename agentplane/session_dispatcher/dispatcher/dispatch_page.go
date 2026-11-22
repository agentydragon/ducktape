package dispatcher

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/resumetoken"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func dispatchPageDigest(sessionID string, caller store.Caller, frontier int64) [32]byte {
	scope := sessionWatchDigest(caller)
	data, _ := json.Marshal(struct {
		Resource, Session string
		Scope             [32]byte
		Frontier          int64
	}{"session-dispatch-page-v1", sessionID, scope, frontier})
	return resumetoken.Digest(data)
}

func (s *Service) ListSessionDispatches(ctx context.Context, request *dispatcherpb.ListSessionDispatchesRequest) (*dispatcherpb.ListSessionDispatchesResponse, error) {
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	id := request.GetSessionId()
	if err := required(id); err != nil {
		return nil, err
	}
	limit := request.GetLimit()
	if limit > 100 {
		return nil, status.Error(codes.InvalidArgument, "limit must be at most 100")
	}
	if limit == 0 {
		limit = 25
	}
	var cursor store.DispatchPageCursor
	if token := request.GetPageToken(); token != "" {
		if len(token) > 80 {
			return nil, status.Error(codes.InvalidArgument, "invalid dispatch page token")
		}
		high, position, ok := strings.Cut(token, ".")
		frontier, err := strconv.ParseInt(high, 10, 64)
		if !ok || err != nil || frontier < 0 {
			return nil, status.Error(codes.InvalidArgument, "invalid dispatch page token")
		}
		before, err := resumetoken.Decode(position, dispatchPageDigest(id, caller, frontier))
		if err != nil || before <= 0 {
			return nil, status.Error(codes.InvalidArgument, "invalid dispatch page token")
		}
		cursor = store.DispatchPageCursor{Before: before, Frontier: frontier}
	}
	rows, next, err := s.Store.ReadDispatchPage(ctx, id, caller, int(limit), cursor)
	if err != nil {
		return nil, apiError(err)
	}
	response := &dispatcherpb.ListSessionDispatchesResponse{Dispatches: rows, Coverage: dispatcherpb.ListSessionDispatchesResponse_RETAINED_DISPATCH_ROWS}
	if next.Before != 0 {
		response.NextPageToken = strconv.FormatInt(next.Frontier, 10) + "." + resumetoken.Encode(next.Before, dispatchPageDigest(id, caller, next.Frontier))
	}
	return response, nil
}
