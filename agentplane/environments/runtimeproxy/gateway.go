package runtimeproxy

import (
	"fmt"
	"time"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/grpc"
	"k8s.io/client-go/util/workqueue"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Config contains the per-replica Environments proxy wiring. Callers use exact service SVID grants;
// runner connections use explicitly configured transport security and incarnation-fenced metadata.
type Config struct {
	RuntimeReaders     []string
	SessionServices    []string
	ControllerServices []string
	ExecutorServices   []string
	RunnerSecurity     peertls.Security
	Namespace          string
	ControlTiming      ControlTiming
}

// ControlTiming bounds Runtime observation liveness.
type ControlTiming struct {
	LivenessTimeout time.Duration
}

func defaultControlTiming(timing ControlTiming) ControlTiming {
	if timing.LivenessTimeout == 0 {
		timing.LivenessTimeout = 30 * time.Second
	}
	return timing
}

func validateControlTiming(timing ControlTiming) error {
	if timing.LivenessTimeout <= 0 {
		return fmt.Errorf("Gateway control timings must be positive")
	}
	return nil
}

// Gateway serves the per-Sandbox Runtime proxy and dials authenticated Runtimes.
// Environments owns the receiver and its independent Runtime observation.
type Gateway struct {
	config      Config
	sandboxes   *sandboxResolver
	connections *connections
	objects     client.Reader
	// observerQueue holds the names of Sandboxes whose observer RunRunnerObservers must reconcile.
	observerQueue workqueue.TypedRateLimitingInterface[string]
}

// New builds a Gateway reading Sandboxes, Pods and KubeVirt objects through objects, which in production is
// an informer cache whose health is objectsHealth.
func New(
	config Config,
	objects client.Reader,
	objectsHealth *watchhealth.Health,
) (*Gateway, error) {
	if err := config.RunnerSecurity.Validate(); err != nil {
		return nil, fmt.Errorf("runner security: %w", err)
	}
	if config.Namespace == "" {
		return nil, fmt.Errorf("Runtime proxy namespace is required")
	}
	config.ControlTiming = defaultControlTiming(config.ControlTiming)
	if err := validateControlTiming(config.ControlTiming); err != nil {
		return nil, err
	}
	sandboxes, err := newSandboxResolver(config.Namespace, objects, objectsHealth)
	if err != nil {
		return nil, err
	}
	return &Gateway{
		config: config, objects: objects,
		sandboxes: sandboxes, connections: newConnections(),
		observerQueue: workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[string]()),
	}, nil
}

// Methods admits callers of the Environment Runtime proxy and the typed execution API. Only the Sandbox
// controller may quiesce or change Firecracker suspension admission; with no controller configured, nobody may.
func (g *Gateway) Methods() (auth.Methods, error) {
	callers, err := auth.Services(g.config.RuntimeReaders)
	if err != nil {
		return nil, err
	}
	dispatcher, err := auth.Services(g.config.SessionServices)
	if err != nil {
		return nil, err
	}
	controllerRule, err := auth.Services(g.config.ControllerServices)
	if err != nil {
		return nil, err
	}
	executors, err := auth.Services(g.config.ExecutorServices)
	if err != nil {
		return nil, err
	}
	return auth.Methods{
		environmentruntimepb.EnvironmentRuntime_ListSessions_FullMethodName:          callers,
		environmentruntimepb.EnvironmentRuntime_GetStatus_FullMethodName:             callers,
		environmentruntimepb.EnvironmentRuntime_CreateSession_FullMethodName:         dispatcher,
		environmentruntimepb.EnvironmentRuntime_SubmitInput_FullMethodName:           dispatcher,
		environmentruntimepb.EnvironmentRuntime_ChangeModel_FullMethodName:           dispatcher,
		environmentruntimepb.EnvironmentRuntime_ChangeReasoningEffort_FullMethodName: dispatcher,
		environmentruntimepb.EnvironmentRuntime_InterruptTurn_FullMethodName:         dispatcher,
		environmentruntimepb.EnvironmentRuntime_Quiesce_FullMethodName:               controllerRule,
		environmentruntimepb.EnvironmentRuntime_PrepareSuspend_FullMethodName:        controllerRule,
		environmentruntimepb.EnvironmentRuntime_ResumeAdmission_FullMethodName:       controllerRule,
		executorpb.Executor_OpenSession_FullMethodName:                               executors,
		executorpb.Executor_StartProcess_FullMethodName:                              executors,
		executorpb.Executor_ReadProcess_FullMethodName:                               executors,
		executorpb.Executor_WriteProcess_FullMethodName:                              executors,
		executorpb.Executor_SignalProcess_FullMethodName:                             executors,
		executorpb.Executor_TerminateProcess_FullMethodName:                          executors,
		executorpb.Executor_GetFileMetadata_FullMethodName:                           executors,
		executorpb.Executor_OpenFile_FullMethodName:                                  executors,
		executorpb.Executor_ReadFile_FullMethodName:                                  executors,
		executorpb.Executor_CloseFile_FullMethodName:                                 executors,
	}, nil
}

// RegisterProxy installs the per-Sandbox Environment Runtime proxy.
func (g *Gateway) RegisterProxy(server *grpc.Server) {
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, &environmentRuntimeProxy{gateway: g})
}

// RegisterExecutor enables the typed execution path only when its own caller allowlist is
// configured. Environments keeps the RPC absent without an Executor grant.
func (g *Gateway) RegisterExecutor(server *grpc.Server) {
	if len(g.config.ExecutorServices) == 0 {
		return
	}
	executorpb.RegisterExecutorServer(server, &executorServer{gateway: g})
}
