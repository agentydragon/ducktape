package sandboxes

import (
	"context"
	"errors"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"google.golang.org/grpc"
)

// EnvironmentService is the generated EnvironmentService get method used by app composition.
type EnvironmentService interface {
	GetEnvironment(context.Context, *environmentpb.GetEnvironmentRequest, ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error)
	WatchEnvironment(context.Context, *environmentpb.WatchEnvironmentRequest, ...grpc.CallOption) (grpc.ServerStreamingClient[environmentpb.WatchEnvironmentResponse], error)
}

// EnvironmentObservation is one authorized watch snapshot or its terminal stream error.
type EnvironmentObservation struct {
	Descriptor *environmentpb.EnvironmentDescriptor
	Err        error
}

// Watch returns the service's exact-target snapshots. Stream errors end the observation;
// the HTTP stream consumer ends too, so its normal reconnect starts a fresh authorized watch.
func (r *EnvironmentReader) Watch(ctx context.Context, target *identitypb.EnvironmentTarget) <-chan EnvironmentObservation {
	observations := make(chan EnvironmentObservation)
	go func() {
		defer close(observations)
		send := func(observation EnvironmentObservation) bool {
			select {
			case observations <- observation:
				return true
			case <-ctx.Done():
				return false
			}
		}
		stream, err := r.WatchEnvironment(ctx, &environmentpb.WatchEnvironmentRequest{Target: target})
		if err != nil {
			send(EnvironmentObservation{Err: err})
			return
		}
		for {
			frame, err := stream.Recv()
			if err != nil {
				send(EnvironmentObservation{Err: err})
				return
			}
			if frame.GetEnvironment() == nil {
				send(EnvironmentObservation{Err: errors.New("Environment watch returned an empty snapshot")})
				return
			}
			if !send(EnvironmentObservation{Descriptor: frame.GetEnvironment()}) {
				return
			}
		}
	}()
	return observations
}

// EnvironmentReader forwards exact-target gets and watches. The service
// owns target validation, authorization and incarnation fencing.
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
