// Package historydata reads the generated History Service API and validates its cursor windows.
package historydata

import (
	"context"
	"errors"
	"fmt"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var (
	ErrInvalidPage      = errors.New("History Service returned an invalid page")
	ErrInvalidSessionID = errors.New("History Service returned an invalid Session ID")
)

// Reader is the generated History Service read boundary. The concrete client is
// historyclient.Client; tests can provide the same generated-message contract.
type Reader interface {
	ReadSessionObservations(context.Context, *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error)
	GetSessionSummary(context.Context, *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error)
	BatchGetSessionSummaries(context.Context, *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error)
	FindSessions(context.Context, *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error)
}

type Store struct{ reader Reader }

func New(reader Reader) (*Store, error) {
	if reader == nil {
		return nil, errors.New("History Service reader is required")
	}
	return &Store{reader: reader}, nil
}

type FeedEnd struct{ Message string }

type FeedSnapshot struct {
	Status   historypb.FeedStatus
	Attached *sessionpb.Attached
	End      *FeedEnd
}

type FoldSummary struct {
	Present             bool
	Sandbox             string
	SandboxUID          string
	ProjectionEpoch     *string
	CommittedCursor     uint64
	FoldedThrough       uint64
	FirstEventAt        *time.Time
	LastEventAt         *time.Time
	LastModelActivityAt *time.Time
	IdleSince           *time.Time
	LastTurnStatus      *int32
	Feed                *FeedSnapshot
}

func (s *Store) GetSummary(ctx context.Context, id uuid.UUID) (FoldSummary, error) {
	message, err := s.reader.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: id.String()})
	if status.Code(err) == codes.NotFound {
		return FoldSummary{}, nil
	}
	if err != nil {
		return FoldSummary{}, err
	}
	return foldSummary(message)
}

func (s *Store) BatchGetSummaries(ctx context.Context, ids []uuid.UUID) (map[uuid.UUID]FoldSummary, error) {
	result := make(map[uuid.UUID]FoldSummary, len(ids))
	for start := 0; start < len(ids); start += 200 {
		end := min(start+200, len(ids))
		batch := make([]string, 0, end-start)
		for _, id := range ids[start:end] {
			batch = append(batch, id.String())
			result[id] = FoldSummary{}
		}
		response, err := s.reader.BatchGetSessionSummaries(ctx, &historypb.BatchGetSessionSummariesRequest{SessionIds: batch})
		if err != nil {
			return nil, err
		}
		for _, message := range response.GetSummaries() {
			id, err := canonicalUUID(message.GetSessionId())
			if err != nil {
				return nil, err
			}
			view, err := foldSummary(message)
			if err != nil {
				return nil, err
			}
			result[id] = view
		}
	}
	return result, nil
}

func (s *Store) FindSessions(ctx context.Context, sandboxName, sandboxNameContains, sandboxUID *string) (map[uuid.UUID]string, error) {
	request := &historypb.FindSessionsRequest{}
	switch {
	case sandboxName != nil:
		request.Sandbox = &historypb.FindSessionsRequest_SandboxName{SandboxName: *sandboxName}
	case sandboxNameContains != nil:
		request.Sandbox = &historypb.FindSessionsRequest_SandboxNameContains{SandboxNameContains: *sandboxNameContains}
	case sandboxUID != nil:
		request.Sandbox = &historypb.FindSessionsRequest_SandboxUid{SandboxUid: *sandboxUID}
	default:
		return nil, errors.New("History Session lookup requires one Sandbox filter")
	}
	response, err := s.reader.FindSessions(ctx, request)
	if err != nil {
		return nil, err
	}
	result := make(map[uuid.UUID]string, len(response.GetSessions()))
	for _, item := range response.GetSessions() {
		id, err := canonicalUUID(item.GetSessionId())
		if err != nil {
			return nil, err
		}
		if item.GetSandbox() == nil || item.GetSandbox().GetName() == "" {
			return nil, fmt.Errorf("%w: Session %s has no Sandbox identity", ErrInvalidPage, id)
		}
		result[id] = item.GetSandbox().GetName()
	}
	return result, nil
}

func canonicalUUID(value string) (uuid.UUID, error) {
	id, err := uuid.Parse(value)
	if err != nil || id.String() != value {
		return uuid.Nil, fmt.Errorf("%w: %q", ErrInvalidSessionID, value)
	}
	return id, nil
}

func foldSummary(message *historypb.SessionSummary) (FoldSummary, error) {
	if message == nil {
		return FoldSummary{}, fmt.Errorf("%w: empty summary", ErrInvalidPage)
	}
	view := FoldSummary{Present: true, CommittedCursor: message.GetCommittedCursor(), FoldedThrough: message.GetFoldedThrough()}
	if message.ProjectionEpoch != nil {
		epoch := message.GetProjectionEpoch()
		view.ProjectionEpoch = &epoch
	}
	if sandbox := message.GetSandbox(); sandbox != nil {
		view.Sandbox = sandbox.GetName()
		view.SandboxUID = sandbox.GetUid()
	}
	view.FirstEventAt = timestamp(message.GetFirstEventAt())
	view.LastEventAt = timestamp(message.GetLastEventAt())
	view.LastModelActivityAt = timestamp(message.GetLastModelActivityAt())
	view.IdleSince = timestamp(message.GetIdleSince())
	if message.LastTurnStatus != nil {
		value := int32(message.GetLastTurnStatus())
		view.LastTurnStatus = &value
	}
	if message.GetAttached() != nil {
		feed := &FeedSnapshot{Status: message.GetFeedStatus(), Attached: message.GetAttached()}
		switch message.GetFeedStatus() {
		case historypb.FeedStatus_FEED_STATUS_ACTIVE:
		case historypb.FeedStatus_FEED_STATUS_ENDED:
			feed.End = &FeedEnd{}
		case historypb.FeedStatus_FEED_STATUS_FAILED:
			feed.End = &FeedEnd{Message: message.GetFeedError()}
		default:
			return FoldSummary{}, fmt.Errorf("%w: attached Session summary has unspecified feed status", ErrInvalidPage)
		}
		view.Feed = feed
	}
	return view, nil
}

func timestamp(value *timestamppb.Timestamp) *time.Time {
	if value == nil || !value.IsValid() {
		return nil
	}
	converted := value.AsTime().UTC()
	return &converted
}

func (s *Store) ReadWatermark(ctx context.Context, id uuid.UUID) (uint64, error) {
	page, err := s.reader.ReadSessionObservations(ctx, &historypb.ReadSessionObservationsRequest{SessionId: id.String(), Limit: 1})
	if status.Code(err) == codes.NotFound {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	return page.GetLastCursor(), nil
}

type Scope struct {
	ProjectionEpoch string
	ThroughCursor   uint64
}

func (s *Store) CurrentScope(ctx context.Context, id uuid.UUID) (*Scope, error) {
	message, err := s.reader.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: id.String()})
	if status.Code(err) == codes.NotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if message.ProjectionEpoch == nil {
		return nil, nil
	}
	return &Scope{ProjectionEpoch: message.GetProjectionEpoch(), ThroughCursor: message.GetFoldedThrough()}, nil
}
