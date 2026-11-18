package runtimeproxy

import (
	"context"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

var identityMetadataKeys = []string{
	sandboxapi.RuntimeProxySandboxNamespaceMetadataKey,
	sandboxapi.RuntimeProxySandboxUIDMetadataKey,
	sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey,
	sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey,
}

type environmentRuntimeProxy struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	server *Server
}

// connection is this replica's authenticated Runtime connection for the Sandbox the call names. Server.Methods
// admitted the caller.
func (p *environmentRuntimeProxy) connection(ctx context.Context, writes, allowSuspending bool) (*runtimeConnection, error) {
	g := p.server
	metadata, _ := metadata.FromIncomingContext(ctx)
	names := metadata.Get(sandboxapi.RuntimeProxySandboxMetadataKey)
	if len(names) != 1 || names[0] == "" {
		return nil, status.Errorf(codes.InvalidArgument, "exactly one %s header is required", sandboxapi.RuntimeProxySandboxMetadataKey)
	}
	expectedIncarnationUIDs := metadata.Get(sandboxapi.RuntimeProxyIncarnationUIDMetadataKey)
	if len(expectedIncarnationUIDs) > 1 || len(expectedIncarnationUIDs) == 1 && expectedIncarnationUIDs[0] == "" {
		return nil, status.Errorf(codes.InvalidArgument, "%s header must be non-empty and appear at most once", sandboxapi.RuntimeProxyIncarnationUIDMetadataKey)
	}
	values := make(map[string][]string, len(identityMetadataKeys))
	for _, key := range identityMetadataKeys {
		values[key] = metadata.Get(key)
	}
	for _, key := range identityMetadataKeys {
		if len(values[key]) != 1 || values[key][0] == "" {
			return nil, status.Error(codes.InvalidArgument, "Sandbox UID and owner headers must each appear exactly once")
		}
	}
	view, err := g.sandboxes.destination(ctx, &identitypb.SandboxDestination{
		Target: &identitypb.EnvironmentTarget{
			Namespace: values[sandboxapi.RuntimeProxySandboxNamespaceMetadataKey][0],
			Name:      names[0], Uid: values[sandboxapi.RuntimeProxySandboxUIDMetadataKey][0],
		},
		Owner: &identitypb.ServiceAccount{
			Namespace: values[sandboxapi.RuntimeProxySandboxOwnerNamespaceMetadataKey][0],
			Name:      values[sandboxapi.RuntimeProxySandboxOwnerNameMetadataKey][0],
		},
	}, writes, allowSuspending)
	if err != nil {
		trace.SpanFromContext(ctx).SetAttributes(attribute.String("runtime.proxy.route", "destination_unavailable"))
		return nil, unavailable(err)
	}
	if len(expectedIncarnationUIDs) == 1 && expectedIncarnationUIDs[0] != view.incarnation.UID {
		trace.SpanFromContext(ctx).SetAttributes(attribute.String("runtime.proxy.route", "expected_incarnation_mismatch"))
		return nil, status.Error(codes.Aborted, "Sandbox Environment Runtime incarnation changed")
	}
	connection, err := g.connections.holder(view.identity.Uid)
	if err != nil {
		trace.SpanFromContext(ctx).SetAttributes(attribute.String("runtime.proxy.route", "connection_unavailable"))
		return nil, err
	}
	if view.incarnation == nil || !proto.Equal(connection.identity, view.identity) || connection.podUID != view.incarnation.UID {
		trace.SpanFromContext(ctx).SetAttributes(attribute.String("runtime.proxy.route", "incarnation_mismatch"))
		return nil, status.Error(codes.Unavailable, "Environment Runtime connection belongs to another incarnation")
	}
	trace.SpanFromContext(ctx).SetAttributes(attribute.String("runtime.proxy.route", "forwarded"))
	return connection, nil
}

func (p *environmentRuntimeProxy) GetStatus(ctx context.Context, request *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	connection, err := p.connection(ctx, false, false)
	if err != nil {
		return nil, err
	}
	snapshot, err := connection.runtime.GetStatus(ctx, request)
	if err != nil {
		return nil, err
	}
	return runtimeDiagnostics(snapshot), nil
}

func (p *environmentRuntimeProxy) Quiesce(ctx context.Context, request *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
	// The controller may have published Quiescing while this Server still caches the preceding
	// SuspendPending phase. Only this controller-authorized control call can use that phase.
	connection, err := p.connection(ctx, false, true)
	if err != nil {
		return nil, err
	}
	return connection.runtime.Quiesce(ctx, request)
}

func (p *environmentRuntimeProxy) PrepareSuspend(ctx context.Context, request *environmentruntimepb.PrepareSuspendRequest) (*emptypb.Empty, error) {
	connection, err := p.connection(ctx, false, true)
	if err != nil {
		return nil, err
	}
	return connection.runtime.PrepareSuspend(ctx, request)
}

func (p *environmentRuntimeProxy) ResumeAdmission(ctx context.Context, request *environmentruntimepb.ResumeAdmissionRequest) (*emptypb.Empty, error) {
	connection, err := p.connection(ctx, false, true)
	if err != nil {
		return nil, err
	}
	return connection.runtime.ResumeAdmission(ctx, request)
}

var _ environmentruntimepb.EnvironmentRuntimeServer = (*environmentRuntimeProxy)(nil)
