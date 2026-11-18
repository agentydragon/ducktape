package dispatcher

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// WatchActivity owns one connection and never decodes a Session before validating the wire version.
func (r *HarnessHostClient) WatchActivity(ctx context.Context, sandbox SandboxSnapshot, apply func(*harnesshostpb.WatchSessionsResponse) error) error {
	target, err := r.proxyTarget(sandbox)
	if err != nil {
		return err
	}
	conn, err := bearer.Dial(target.address, target.tokenFile, r.Timeout, target.tls)
	if err != nil {
		return err
	}
	defer conn.Close()
	return watchActivity(metadata.NewOutgoingContext(ctx, target.metadata), conn, r.Timeout, apply)
}

type CentralActivityClient struct {
	Target  *CentralTarget
	Timeout time.Duration
}

func (c *CentralActivityClient) WatchActivity(ctx context.Context, apply func(*harnesshostpb.WatchSessionsResponse) error) error {
	conn, err := bearer.Dial(c.Target.Target, c.Target.TokenFile, c.Timeout, c.Target.TLS)
	if err != nil {
		return err
	}
	defer conn.Close()
	return watchActivity(metadata.NewOutgoingContext(ctx, metadata.MD{}), conn, c.Timeout, apply)
}

// Both hosts validate headers before Recv, so incompatible Session bytes are never decoded.
func watchActivity(ctx context.Context, conn *grpc.ClientConn, timeout time.Duration, apply func(*harnesshostpb.WatchSessionsResponse) error) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	// Only connection establishment and headers have a deadline; the event stream stays live.
	headerTimeout := time.AfterFunc(timeout, cancel)
	defer headerTimeout.Stop()
	stream, err := harnesshostpb.NewHarnessHostClient(conn).WatchSessions(ctx, &harnesshostpb.WatchSessionsRequest{})
	if err != nil {
		return err
	}
	header, err := stream.Header()
	if err != nil {
		return err
	}
	if !headerTimeout.Stop() {
		return context.DeadlineExceeded
	}
	if err := harnesshost.RequireSessionVersion(header); err != nil {
		return err
	}
	for {
		frame, err := stream.Recv()
		if err != nil {
			return err
		}
		if err := apply(frame); err != nil {
			return err
		}
	}
}
