// Package historydata reads the generated History Service API and validates its cursor windows.
package historydata

import (
	"context"
	"errors"
	"fmt"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
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

// GetSummary returns nil when the Session has no recorded History.
func (s *Store) GetSummary(ctx context.Context, id uuid.UUID) (*historypb.SessionSummary, error) {
	message, err := s.reader.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: id.String()})
	if status.Code(err) == codes.NotFound {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err := validateSummary(message); err != nil {
		return nil, err
	}
	return message, nil
}

func (s *Store) BatchGetSummaries(ctx context.Context, ids []uuid.UUID) (map[uuid.UUID]*historypb.SessionSummary, error) {
	result := make(map[uuid.UUID]*historypb.SessionSummary, len(ids))
	for start := 0; start < len(ids); start += 200 {
		end := min(start+200, len(ids))
		batch := make([]string, 0, end-start)
		for _, id := range ids[start:end] {
			batch = append(batch, id.String())
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
			if err := validateSummary(message); err != nil {
				return nil, err
			}
			result[id] = message
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

func validateSummary(message *historypb.SessionSummary) error {
	if message == nil {
		return fmt.Errorf("%w: empty summary", ErrInvalidPage)
	}
	if message.GetAttached() != nil {
		switch message.GetFeedStatus() {
		case historypb.FeedStatus_FEED_STATUS_UNSPECIFIED, historypb.FeedStatus_FEED_STATUS_ACTIVE,
			historypb.FeedStatus_FEED_STATUS_ENDED, historypb.FeedStatus_FEED_STATUS_FAILED:
		default:
			return fmt.Errorf("%w: unknown feed status %d", ErrInvalidPage, message.GetFeedStatus())
		}
	}
	return nil
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

// CurrentEpoch reports the fold epoch when History has materialized the Session.
func (s *Store) CurrentEpoch(ctx context.Context, id uuid.UUID) (string, bool, error) {
	message, err := s.reader.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: id.String()})
	if status.Code(err) == codes.NotFound {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	if message.ProjectionEpoch == nil {
		return "", false, nil
	}
	return message.GetProjectionEpoch(), true, nil
}
