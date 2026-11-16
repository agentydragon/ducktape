// Package historydata reads and validates Session metadata from the generated History Service API.
package historydata

import (
	"context"
	"errors"
	"fmt"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

var ErrInvalidPage = errors.New("History Service returned an invalid page")

// Reader is the generated History Service read boundary. The concrete client is
// historyclient.Client; tests can provide the same generated-message contract.
type Reader interface {
	GetSessionSummary(context.Context, *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error)
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
