package dispatcher

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/harness_host"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/metadata"
)

// WatchActivity owns one connection and never decodes a Session before validating the wire version.
func (r *RuntimeProxyClient) WatchActivity(ctx context.Context, sandbox SandboxSnapshot, apply func(*harnesshostpb.WatchSessionsResponse) error) error {
	if sandbox.Name == "" || sandbox.UID == "" || sandbox.Namespace != r.Namespace {
		return errors.New("complete in-namespace Sandbox identity is required")
	}
	conn, err := bearer.Dial(r.target(sandbox.Name), r.TokenFile, r.Timeout, r.GatewayTLS)
	if err != nil {
		return err
	}
	defer conn.Close()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs(
		sandboxapi.RuntimeProxySandboxMetadataKey, sandbox.Name,
		sandboxapi.RuntimeProxySandboxNamespaceMetadataKey, sandbox.Namespace,
		sandboxapi.RuntimeProxySandboxUIDMetadataKey, sandbox.UID,
		sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey, sandbox.Namespace,
		sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey, sandbox.Name,
	))
	// Only connection establishment and headers have a deadline; the event stream stays live.
	timeout := time.AfterFunc(r.Timeout, cancel)
	defer timeout.Stop()
	stream, err := harnesshostpb.NewHarnessHostClient(conn).WatchSessions(ctx, &harnesshostpb.WatchSessionsRequest{})
	if err != nil {
		return err
	}
	header, err := stream.Header()
	if err != nil {
		return err
	}
	if !timeout.Stop() {
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
