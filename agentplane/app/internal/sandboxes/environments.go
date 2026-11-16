package sandboxes

import (
	"context"
	"errors"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"google.golang.org/grpc"
)

// EnvironmentService exposes the generated get method used by app composition.
type EnvironmentService interface {
	GetEnvironment(context.Context, *environmentpb.GetEnvironmentRequest, ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error)
}

// EnvironmentReader forwards exact-target gets. The service owns target validation,
// authorization and incarnation fencing.
type EnvironmentReader struct {
	EnvironmentService
}

func NewEnvironmentReader(service EnvironmentService) (*EnvironmentReader, error) {
	if service == nil {
		return nil, errors.New("Environment Service is required")
	}
	return &EnvironmentReader{EnvironmentService: service}, nil
}

var _ EnvironmentService = environmentpb.EnvironmentServiceClient(nil)
