package connections

import (
	"bytes"
	"cmp"
	"context"
	"errors"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

const (
	// snapshotChunk bounds the Connections in one snapshot frame.
	snapshotChunk = 64
	// recheckInterval is how often a watch with nothing to send checks its bearer again.
	recheckInterval = 5 * time.Second
	// watchWriteTimeout bounds waiting for a watch Send to clear transport backpressure.
	watchWriteTimeout = 5 * time.Second
)

// WatchConnections sends every Connection, then each one again whenever it changes. A NOTIFY only wakes the watch:
// it reads every Connection's version, and the Connections whose version differs from the one the client holds. The
// wakeup that follows a reconnection of the LISTEN connection is one too, so a change made while it was down is sent
// late, never lost. The bearer is checked again before every frame and every recheckInterval while idle, so the
// stream ends UNAUTHENTICATED once its bearer expires. An idle stream renews with reconnect_required when the
// service drains; if drain ends an outstanding write, the stream ends without another frame.
func (s *Service) WatchConnections(_ *pb.WatchConnectionsRequest, stream grpc.ServerStreamingServer[pb.WatchConnectionsResponse]) error {
	ctx := stream.Context()
	// Subscribed before the first read, so a commit after it wakes the watch.
	updates, unsubscribe, err := s.updates.Subscribe(UpdatesChannel)
	if err != nil {
		return failure(ctx, err)
	}
	defer unsubscribe()
	writeCtx, stopWrites := context.WithCancel(ctx)
	stopOnDrain := context.AfterFunc(s.gate.DrainingContext(), stopWrites)
	defer func() {
		stopOnDrain()
		stopWrites()
	}()
	w := watch{service: s, ctx: ctx, writeCtx: writeCtx, stream: stream, shown: map[uuid.UUID]int64{}}
	if err := w.snapshot(); err != nil {
		return failure(ctx, err)
	}
	recheck := time.NewTicker(recheckInterval)
	defer recheck.Stop()
	for {
		select {
		case <-updates:
			if err := w.upsertChanges(); err != nil {
				return failure(ctx, err)
			}
		case <-s.gate.DrainingContext().Done():
			// A planned renewal: the client connects again, to a replica that is not going away.
			return failure(ctx, w.sendReconnect(&pb.WatchConnectionsResponse{
				Frame: &pb.WatchConnectionsResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
			}))
		case <-ctx.Done():
			return failure(ctx, ctx.Err())
		case <-recheck.C:
			if err := s.gate.Recheck(ctx); err != nil {
				return failure(ctx, err)
			}
		}
	}
}

// watch is one WatchConnections stream.
type watch struct {
	service  *Service
	ctx      context.Context
	writeCtx context.Context
	stream   grpc.ServerStreamingServer[pb.WatchConnectionsResponse]
	// shown is the version of each Connection the client holds.
	shown map[uuid.UUID]int64
}

// send checks the bearer again, then sends the frame.
func (w *watch) send(frame *pb.WatchConnectionsResponse) error {
	if err := w.service.gate.Recheck(w.ctx); err != nil {
		return err
	}
	return w.sendWithin(w.writeCtx, frame)
}

func (w *watch) sendReconnect(frame *pb.WatchConnectionsResponse) error {
	if err := w.service.gate.Recheck(w.ctx); err != nil {
		return err
	}
	return w.sendWithin(w.ctx, frame)
}

func (w *watch) sendWithin(ctx context.Context, frame *pb.WatchConnectionsResponse) error {
	err := streaming.SendWithin(ctx, watchWriteTimeout, w.stream.Send, frame)
	switch {
	case errors.Is(err, streaming.ErrSendTimeout):
		return status.Error(codes.DeadlineExceeded, "Connection watch write deadline expired")
	case errors.Is(err, context.Canceled):
		return status.FromContextError(context.Canceled).Err()
	case errors.Is(err, context.DeadlineExceeded):
		return status.FromContextError(context.DeadlineExceeded).Err()
	default:
		return err
	}
}

// snapshot sends every Connection as snapshot frames of at most snapshotChunk, oldest first, then snapshot_complete.
func (w *watch) snapshot() error {
	var after *position
	for {
		connections, err := w.service.store.list(w.ctx, after, snapshotChunk)
		if err != nil {
			return err
		}
		if len(connections) > 0 {
			views, err := connectionViews(connections)
			if err != nil {
				return err
			}
			if err := w.send(&pb.WatchConnectionsResponse{Frame: &pb.WatchConnectionsResponse_Snapshot{
				Snapshot: &pb.ConnectionChunk{Connections: views},
			}}); err != nil {
				return err
			}
			for _, c := range connections {
				w.shown[c.id] = c.version
			}
		}
		if len(connections) < snapshotChunk {
			break
		}
		last := connections[len(connections)-1].position()
		after = &last
	}
	return w.send(&pb.WatchConnectionsResponse{
		Frame: &pb.WatchConnectionsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}},
	})
}

// upsertChanges sends an upsert of each Connection whose version is not the one the client holds, in the order they
// changed. Only those are read in full.
func (w *watch) upsertChanges() error {
	versions, err := w.service.store.versions(w.ctx)
	if err != nil {
		return err
	}
	var changed []uuid.UUID
	for id, version := range versions {
		if w.shown[id] != version {
			changed = append(changed, id)
		}
	}
	if len(changed) == 0 {
		return nil
	}
	connections, err := w.service.store.find(w.ctx, changed)
	if err != nil {
		return err
	}
	slices.SortFunc(connections, func(a, b connection) int {
		return cmp.Or(a.updatedAt.Compare(b.updatedAt), bytes.Compare(a.id[:], b.id[:]))
	})
	for _, c := range connections {
		view, err := connectionView(c)
		if err != nil {
			return err
		}
		if err := w.send(&pb.WatchConnectionsResponse{Frame: &pb.WatchConnectionsResponse_Upsert{Upsert: view}}); err != nil {
			return err
		}
		w.shown[c.id] = c.version
	}
	return nil
}
