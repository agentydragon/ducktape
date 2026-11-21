package rpc

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

const recordingsPage = 500

type watchStream = grpc.ServerStreamingServer[historypb.WatchSessionSummariesResponse]

// In notification mode, WatchSessionSummaries sends `changed` on recording and after each change, each Session recorded since when the
// request asks for them, and `reconnect_required` when the lease ends or the server drains while idle. If either
// ends during a write, the handler returns without another send. A wakeup only prompts the caller to re-read: what
// it follows is in the tables, so a notification lost on a replica or a dropped LISTEN connection costs at most a
// spurious `changed`.
func (s *Service) WatchSessionSummaries(request *historypb.WatchSessionSummariesRequest, stream watchStream) error {
	if request.GetSelected() != nil {
		if request.RecordedAfter != nil {
			return status.Error(codes.InvalidArgument, "selected summaries cannot be combined with recorded_after")
		}
		return s.watchSelectedSummaries(request.GetSelected(), stream)
	}
	ctx := stream.Context()
	reader, err := s.scopedReader(ctx)
	if err != nil {
		return failure(ctx, err)
	}
	leased, endLease := context.WithTimeout(ctx, s.lease)
	defer endLease()
	defer context.AfterFunc(s.draining, endLease)()
	// Subscribed before the recording notice, so nothing committed after it goes unannounced. A recorded Session
	// also wakes a stream that asked for none: a spurious `changed` is harmless.
	changed, unsubscribe, err := s.wakeups.Subscribe(projection.ChannelSummaries, storage.ChannelSessions)
	if err != nil {
		return failure(ctx, err)
	}
	defer unsubscribe()
	recordedAfter := request.RecordedAfter
	for {
		if err := s.send(leased, stream, &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_Changed{Changed: &emptypb.Empty{}}}); err != nil {
			return err
		}
		if recordedAfter != nil {
			position, err := s.announceSessions(leased, stream, reader, *recordedAfter)
			if err != nil {
				return err
			}
			recordedAfter = &position
		}
		select {
		case <-changed:
		case <-leased.Done():
			if ctx.Err() != nil {
				return status.FromContextError(ctx.Err()).Err()
			}
			// The lease ended or the server is draining; the caller connects again with fresh authorization.
			return s.send(ctx, stream, &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}})
		}
	}
}

// announceSessions sends `session_recorded` for each Session recorded after the position, and returns the last
// position sent.
func (s *Service) announceSessions(ctx context.Context, stream watchStream, reader *reads.Reader, after uint64) (uint64, error) {
	for {
		page, err := s.recordedSessions(ctx, reader, after)
		if err != nil {
			return 0, err
		}
		for _, recorded := range page {
			response := &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_SessionRecorded{
				SessionRecorded: &historypb.SessionRecorded{SessionId: recorded.SessionID.String(), Position: recorded.Position},
			}}
			if err := s.send(ctx, stream, response); err != nil {
				return 0, err
			}
			after = recorded.Position
		}
		if len(page) < recordingsPage {
			return after, nil
		}
	}
}

func (s *Service) recordedSessions(ctx context.Context, reader *reads.Reader, after uint64) ([]storage.RecordedSession, error) {
	ctx, cancel := context.WithTimeout(ctx, s.requestTimeout)
	defer cancel()
	page, err := reader.History.RecordedSessions(ctx, after, recordingsPage)
	if err != nil {
		return nil, failure(ctx, err)
	}
	return page, nil
}

// send writes within the request timeout: a caller that stops reading would otherwise hold the stream open
// past its lease.
func (s *Service) send(ctx context.Context, stream watchStream, response *historypb.WatchSessionSummariesResponse) error {
	return sendWithin(ctx, stream, s.requestTimeout, response)
}

func sendWithin[Response any](ctx context.Context, stream interface{ Send(*Response) error }, timeout time.Duration, response *Response) error {
	return streaming.SendWithinStatus(ctx, timeout, stream.Send, response, "history stream write deadline expired")
}

// watchSelectedSummaries sends an initial snapshot and changed rows, rereading on each
// wakeup and after listener recovery. The subscription precedes the snapshot.
func (s *Service) watchSelectedSummaries(request *historypb.SelectedSessionSummaries, stream watchStream) error {
	ctx := stream.Context()
	reader, err := s.scopedReader(ctx)
	if err != nil {
		return failure(ctx, err)
	}
	leased, cancel := context.WithTimeout(ctx, s.lease)
	defer cancel()
	defer context.AfterFunc(s.draining, cancel)()
	changed, unsubscribe, err := s.wakeups.Subscribe(projection.ChannelSummaries, storage.ChannelSessions)
	if err != nil {
		return failure(ctx, err)
	}
	defer unsubscribe()
	previous := map[string]*historypb.SessionSummary{}
	initial := true
	for {
		if !s.wakeups.Connected() {
			return status.Error(codes.Unavailable, "summary change listener is disconnected")
		}
		readCtx, end := context.WithTimeout(leased, s.requestTimeout)
		result, readErr := reader.BatchGetSessionSummaries(readCtx, &historypb.BatchGetSessionSummariesRequest{SessionIds: request.GetSessionIds()})
		end()
		if readErr != nil {
			return failure(ctx, readErr)
		}
		next := map[string]*historypb.SessionSummary{}
		for _, row := range result.GetSummaries() {
			next[row.GetSessionId()] = row
		}
		if initial {
			if err := sendWithin(leased, stream, s.requestTimeout, &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_Snapshot{Snapshot: result}}); err != nil {
				return err
			}
			initial = false
		} else {
			for id, row := range next {
				if !proto.Equal(row, previous[id]) {
					if err := sendWithin(leased, stream, s.requestTimeout, &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_Summary{Summary: row}}); err != nil {
						return err
					}
				}
			}
			for id := range previous {
				if next[id] == nil {
					if err := sendWithin(leased, stream, s.requestTimeout, &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_RemovedSessionId{RemovedSessionId: id}}); err != nil {
						return err
					}
				}
			}
		}
		previous = next
		select {
		case <-changed:
		case <-leased.Done():
			if ctx.Err() != nil {
				return status.FromContextError(ctx.Err()).Err()
			}
			return sendWithin(ctx, stream, s.requestTimeout, &historypb.WatchSessionSummariesResponse{Observation: &historypb.WatchSessionSummariesResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}}})
		}
	}
}
