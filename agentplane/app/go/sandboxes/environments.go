package sandboxes

import (
	"context"
	"errors"

	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"google.golang.org/grpc"
)

// EnvironmentService is the generated EnvironmentService get method used by app composition.
type EnvironmentService interface {
	GetEnvironment(context.Context, *environmentpb.GetEnvironmentRequest, ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error)
}

// EnvironmentReader forwards exact-target gets using the app's configured namespace. The service
// owns target validation, authorization and incarnation fencing.
type EnvironmentReader struct {
	namespace string
	service   EnvironmentService
}

func NewEnvironmentReader(namespace string, service EnvironmentService) (*EnvironmentReader, error) {
	if namespace == "" || service == nil {
		return nil, errors.New("Sandbox namespace and Environment Service are required")
	}
	return &EnvironmentReader{namespace: namespace, service: service}, nil
}

func (r *EnvironmentReader) GetExactUID(ctx context.Context, name, uid string) (*environmentpb.EnvironmentDescriptor, error) {
	return r.GetExact(ctx, &environmentpb.TargetHandle{Namespace: r.namespace, Name: name, Uid: uid})
}

func (r *EnvironmentReader) GetExact(ctx context.Context, target *environmentpb.TargetHandle) (*environmentpb.EnvironmentDescriptor, error) {
	return r.service.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: target})
}

var _ EnvironmentService = environmentpb.EnvironmentServiceClient(nil)
