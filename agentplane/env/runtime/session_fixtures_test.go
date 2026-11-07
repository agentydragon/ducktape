package runtime

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"google.golang.org/grpc/metadata"
)

// Component tests control History acknowledgements themselves. Runtime still owns
// and joins the delivery worker; ingress_owner_test exercises real delivery.
func newSessionTestRuntime(t *testing.T, config runtimeTestConfig) *Runtime {
	t.Helper()
	return newSessionTestRuntimeWithState(t, config, nil)
}

func newSessionTestRuntimeWithState(t *testing.T, config runtimeTestConfig, resources state.State) *Runtime {
	t.Helper()
	var r *Runtime
	cleanupDelivery := make(chan struct{})
	if config.deps.runIngress == nil {
		config.deps.runIngress = func(ctx context.Context, endpoint configuration.Endpoint) error {
			select {
			case <-cleanupDelivery:
				return r.ingressSupervisor(endpoint, r.fail).Run(ctx)
			case <-ctx.Done():
				return nil
			}
		}
	}
	r = newLifecycleRunnerWithState(t, config, resources)
	startDirectExecutionTestClient(t, r, "test-incarnation")
	// Assertions retain manual ACK control. Only cleanup enables real delivery,
	// which observes and acknowledges seals published later by Runtime.Close.
	t.Cleanup(func() { close(cleanupDelivery) })
	return r
}

func sessionCallContext(r *Runtime, ctx context.Context) context.Context {
	r.mu.Lock()
	uid := r.desired.IncarnationUID
	r.mu.Unlock()
	return metadata.NewIncomingContext(ctx, metadata.Pairs(
		control.SandboxMetadata, r.config.Launch.Sandbox.UID,
		control.IncarnationMetadata, uid,
		control.BootMetadata, r.bootID,
		control.EpochMetadata, "1",
	))
}

func environmentDesired(r *Runtime) lifecycle.EnvironmentDesired {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.desired
}

func currentSessionLifecycleStatus(r *Runtime) lifecycle.Status {
	value, _, err := lifecycle.ReadStatus(context.Background(), r.lifecycleState)
	if err != nil {
		panic(err)
	}
	return value
}

func sessionAdmissionOpen(r *Runtime) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.admissionOpen
}

func watchSessionResource[T lifecycle.Spec[T]](ctx context.Context, resources state.State, id string, condition func(T) (bool, error)) error {
	var zero T
	_, err := safe.StateWatchFor[*typed.Resource[T, lifecycle.Extension[T]]](ctx, resources, lifecycle.ResourceFor(id, zero).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			return condition(*value.(*typed.Resource[T, lifecycle.Extension[T]]).TypedSpec())
		}))
	return err
}
