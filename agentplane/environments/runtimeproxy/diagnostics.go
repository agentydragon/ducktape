package runtimeproxy

import (
	"context"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// RuntimeStatus is Environments' internal diagnostic read on its replica-local transport.
// The caller supplies the incarnation from an uncached Sandbox read; no incoming proxy headers
// or caller credentials are forwarded to the Runtime.
func (g *Server) RuntimeStatus(ctx context.Context, target *identitypb.EnvironmentTarget, incarnationUID string) (*sessionpb.EnvironmentRuntimeStatus, error) {
	if target.GetNamespace() == "" || target.GetName() == "" || target.GetUid() == "" || incarnationUID == "" {
		return nil, status.Error(codes.InvalidArgument, "exact environment and incarnation are required")
	}
	destination := &identitypb.SandboxDestination{Target: target, Owner: &identitypb.ServiceAccount{Namespace: target.Namespace, Name: target.Name}}
	view, err := g.sandboxes.destination(ctx, destination, false, false)
	if err != nil {
		return nil, unavailable(err)
	}
	if view.incarnation.UID != incarnationUID {
		return nil, status.Error(codes.Aborted, "environment incarnation changed")
	}
	connection, err := g.connections.holder(target.Uid)
	if err != nil {
		return nil, err
	}
	if !proto.Equal(connection.identity, target) || connection.podUID != incarnationUID {
		return nil, status.Error(codes.Unavailable, "Runtime connection belongs to another incarnation")
	}
	snapshot, err := connection.runtime.GetStatus(ctx, &sessionpb.GetStatusRequest{})
	if err != nil {
		if ctx.Err() != nil {
			return nil, status.FromContextError(ctx.Err()).Err()
		}
		return nil, status.Error(codes.Unavailable, "Runtime diagnostics read failed")
	}
	if snapshot.GetEnvironmentRuntimeBootId() != connection.bootID || connection.bootID == "" {
		return nil, status.Error(codes.Unavailable, "Runtime diagnostics boot changed")
	}
	current, err := g.connections.holder(target.Uid)
	if err != nil {
		return nil, err
	}
	if current != connection {
		return nil, status.Error(codes.Aborted, "Runtime observation changed")
	}
	return snapshot, nil
}
