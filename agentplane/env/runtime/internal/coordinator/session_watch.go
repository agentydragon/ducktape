package coordinator

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const sessionWatchWriteTimeout = 10 * time.Second

func (r *Runtime) watchSessions(ctx context.Context, send func(*harnesshostpb.WatchSessionsResponse) error) error {
	previous := map[string]*sessionpb.SessionSummary{}
	initial := true
	for {
		// Subscribe before reading so a concurrent update cannot be lost between snapshot and wait.
		changed := r.changesSince()
		summariesChanged := r.sessions.SummaryChangesSince()
		sessions, err := r.listSessions(ctx)
		if err != nil {
			return err
		}
		updated := initial
		for _, session := range sessions {
			if proto.Equal(previous[session.SessionId], session) {
				continue
			}
			frame := &harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_Upsert{Upsert: session}}
			if err := streaming.SendWithin(ctx, sessionWatchWriteTimeout, send, frame); err != nil {
				return err
			}
			previous[session.SessionId] = session
			updated = true
		}
		if updated {
			frame := &harnesshostpb.WatchSessionsResponse{Update: &harnesshostpb.WatchSessionsResponse_CaughtUp{CaughtUp: &harnesshostpb.CaughtUp{}}}
			if err := streaming.SendWithin(ctx, sessionWatchWriteTimeout, send, frame); err != nil {
				return err
			}
		}
		initial = false
		select {
		case <-changed:
		case <-summariesChanged:
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-r.lifecycleContext.Done():
			return status.Error(codes.Unavailable, "Runtime is closing")
		}
	}
}
