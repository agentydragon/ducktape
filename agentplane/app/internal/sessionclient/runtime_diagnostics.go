package sessionclient

import (
	"context"
	"errors"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

// RuntimeDiagnostics uses the composition root's EnvironmentService connection.
type RuntimeDiagnostics struct {
	namespace string
	client    environmentpb.EnvironmentServiceClient
	timeout   time.Duration
}

func NewRuntimeDiagnostics(namespace string, client environmentpb.EnvironmentServiceClient, timeout time.Duration) (*RuntimeDiagnostics, error) {
	if namespace == "" || client == nil || timeout <= 0 {
		return nil, errors.New("environment namespace, EnvironmentService client and positive timeout are required")
	}
	return &RuntimeDiagnostics{namespace: namespace, client: client, timeout: timeout}, nil
}

// EnvironmentRuntimeStatus preserves the App's browser and archive-check snapshot contract.
// The receiver supplies fenced live evidence; errors never fall back to raw Runtime calls.
func (r *RuntimeDiagnostics) EnvironmentRuntimeStatus(ctx context.Context, sandbox *environmentpb.EnvironmentDescriptor) (*sessionpb.EnvironmentRuntimeStatus, error) {
	target := sandbox.GetTarget()
	if target.GetNamespace() != r.namespace || target.GetName() == "" || target.GetUid() == "" {
		return nil, errors.New("complete environment identity in the configured namespace is required")
	}
	ctx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()
	response, err := r.client.GetRuntimeDiagnostics(metadata.NewOutgoingContext(ctx, metadata.MD{}), &environmentpb.GetRuntimeDiagnosticsRequest{Target: target})
	if err != nil {
		return nil, translatedRPCError(err)
	}
	if !proto.Equal(response.GetTarget(), target) || response.GetIncarnationUid() == "" || response.GetObservedAt() == nil || response.GetObservedAt().CheckValid() != nil || response.GetStatus().GetEnvironmentRuntimeBootId() == "" {
		return nil, errors.New("EnvironmentService returned incomplete or mismatched Runtime evidence")
	}
	return response.GetStatus(), nil
}

var _ RuntimeStatusReader = (*RuntimeDiagnostics)(nil)
