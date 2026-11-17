package dispatcher

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// WatchActivity owns one connection and never decodes a Session before validating the wire version.
func (r *HarnessHostClient) WatchActivity(ctx context.Context, sandbox SandboxSnapshot, apply func(*harnesshostpb.WatchSessionsResponse) error) error {
	if sandbox.Name == "" || sandbox.UID == "" || sandbox.Namespace != r.Namespace {
		return errors.New("complete in-namespace Sandbox identity is required")
	}
	conn, err := bearer.Dial(r.target(sandbox.Name), r.TokenFile, r.Timeout, r.GatewayTLS)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs(
		sandboxapi.RuntimeProxySandboxMetadataKey, sandbox.Name,
		sandboxapi.RuntimeProxySandboxNamespaceMetadataKey, sandbox.Namespace,
		sandboxapi.RuntimeProxySandboxUIDMetadataKey, sandbox.UID,
		sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey, sandbox.Namespace,
		sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey, sandbox.Name,
	))
	return watchActivity(ctx, conn, r.Timeout, apply)
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
