package reads

import (
	"context"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
)

// ReadSessionEvents pages through a Session's retained runner entries.
func (r *Reader) ReadSessionEvents(ctx context.Context, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	sessionID, err := parseSessionID(request.GetSessionId())
	if err != nil {
		return nil, err
	}
	return r.History.ReadPage(ctx, sessionID, request.GetAfterCursor(), int(request.GetLimit()))
}

// ReadSessionObservations reads a window of a Session's observation metadata, or its tail when neither bound is set.
func (r *Reader) ReadSessionObservations(ctx context.Context, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	sessionID, err := parseSessionID(request.GetSessionId())
	if err != nil {
		return nil, err
	}
	page, err := r.History.ReadObservations(ctx, sessionID, request.BeforeCursor, request.AfterCursor, int(request.GetLimit()))
	if err != nil {
		return nil, err
	}
	response := &historypb.ReadSessionObservationsResponse{LastCursor: page.LastCursor}
	for _, observation := range page.Observations {
		response.Observations = append(response.Observations, &historypb.SessionObservation{Cursor: observation.Cursor, Kind: observation.Kind})
	}
	for _, settled := range page.Settled {
		response.Settled = append(response.Settled, &historypb.CursorRange{First: settled.First, Last: settled.Last})
	}
	return response, nil
}
