package connections

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	pb "github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

// errDraining ends a watch whose write the drain cut short; the client connects again, as after reconnect_required.
var errDraining = status.Error(codes.Unavailable, "the Connection service is draining")

// WatchConnections sends every Connection, oldest first, then each one again whenever it changes, as a
// streaming.VersionedSetWatch: the stream ends UNAUTHENTICATED once its bearer expires, and an idle one renews with
// reconnect_required when the service drains.
func (s *Service) WatchConnections(_ *pb.WatchConnectionsRequest, stream grpc.ServerStreamingServer[pb.WatchConnectionsResponse]) error {
	ctx := stream.Context()
	updates, unsubscribe, err := s.updates.Subscribe(UpdatesChannel)
	if err != nil {
		return failure(ctx, err)
	}
	defer unsubscribe()
	watch := streaming.VersionedSetWatch[connection, pb.WatchConnectionsResponse]{
		Gate:           s.gate,
		Updates:        updates,
		Send:           stream.Send,
		TimeoutMessage: "Connection watch write deadline expired",
		Draining:       errDraining,
		List: func(ctx context.Context, after *connection, limit int) ([]connection, error) {
			if after == nil {
				return s.store.list(ctx, nil, limit)
			}
			last := after.position()
			return s.store.list(ctx, &last, limit)
		},
		Versions: s.store.versions,
		Find:     s.store.find,
		Key: func(c connection) streaming.Versioned {
			return streaming.Versioned{ID: c.id, Version: c.version, UpdatedAt: c.updatedAt}
		},
		Snapshot: func(connections []connection) (*pb.WatchConnectionsResponse, error) {
			views, err := connectionViews(connections)
			if err != nil {
				return nil, err
			}
			return &pb.WatchConnectionsResponse{Frame: &pb.WatchConnectionsResponse_Snapshot{
				Snapshot: &pb.ConnectionChunk{Connections: views},
			}}, nil
		},
		SnapshotComplete: &pb.WatchConnectionsResponse{
			Frame: &pb.WatchConnectionsResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}},
		},
		Upsert: func(c connection) (*pb.WatchConnectionsResponse, error) {
			view, err := connectionView(c)
			if err != nil {
				return nil, err
			}
			return &pb.WatchConnectionsResponse{Frame: &pb.WatchConnectionsResponse_Upsert{Upsert: view}}, nil
		},
		ReconnectRequired: &pb.WatchConnectionsResponse{
			Frame: &pb.WatchConnectionsResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
		},
	}
	return failure(ctx, watch.Run(ctx))
}
