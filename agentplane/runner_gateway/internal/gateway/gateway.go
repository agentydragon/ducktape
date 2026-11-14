package gateway

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

// Config contains the per-process Gateway role wiring. Public callers use TokenReview;
// runner connections use explicitly configured transport security and incarnation-fenced metadata.
type Config struct {
	RuntimeReaders     []string
	SessionServices    []string
	ControllerServices []string
	ExecutorServices   []string
	RunnerSecurity     peertls.Security
	Namespace          string
	CallerAccounts     []auth.Account
	// DispatcherAccount is the combined Gateway/Dispatcher workload. Its Pod-bound
	// projected token is the only public route to Runtime Session creation and commands.
	DispatcherAccount        auth.Account
	SandboxControllerAccount *auth.Account
	ControlTiming            ControlTiming
	// ExecutorCallers enables the typed execution API independently from the Runner proxy's
	// caller allowlist. An empty set leaves the public service unregistered.
	ExecutorCallers []auth.Account
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

// Gateway serves the per-Sandbox Runner proxy and dials each Runner's authenticated gRPC server.
type Gateway struct {
	config      Config
	callerAuth  *auth.Resolver
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
	callerAuth *auth.Resolver,
	objects client.Reader,
	objectsHealth *watchhealth.Health,
) (*Gateway, error) {
	if err := config.RunnerSecurity.Validate(); err != nil {
		return nil, fmt.Errorf("runner security: %w", err)
	}
	if config.Namespace == "" || len(config.CallerAccounts) == 0 || callerAuth == nil {
		return nil, fmt.Errorf("Gateway namespace, caller policy and token resolvers are required")
	}
	if config.DispatcherAccount.Namespace == "" || config.DispatcherAccount.Name == "" {
		return nil, fmt.Errorf("Gateway Dispatcher account is required")
	}
	config.ControlTiming = defaultControlTiming(config.ControlTiming)
	if err := validateControlTiming(config.ControlTiming); err != nil {
		return nil, err
	}
	sandboxes, err := newSandboxResolver(config.Namespace, objects, objectsHealth)
	if err != nil {
		return nil, err
	}
	for _, account := range config.CallerAccounts {
		if account.Namespace == "" || account.Name == "" {
			return nil, fmt.Errorf("Gateway caller account namespace and name are required")
		}
	}
	for _, account := range config.ExecutorCallers {
		if account.Namespace == "" || account.Name == "" {
			return nil, fmt.Errorf("Gateway executor caller account namespace and name are required")
		}
	}
	if config.SandboxControllerAccount != nil {
		if config.SandboxControllerAccount.Namespace == "" || config.SandboxControllerAccount.Name == "" {
			return nil, fmt.Errorf("Sandbox controller account namespace and name are required")
		}
		copy := *config.SandboxControllerAccount
		config.SandboxControllerAccount = &copy
	}
	return &Gateway{
		config: config, callerAuth: callerAuth, objects: objects,
		sandboxes: sandboxes, connections: newConnections(),
		observerQueue: workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[string]()),
	}, nil
}

// Methods admits callers of the Environment Runtime proxy and the typed execution API. Only the Sandbox
// controller may quiesce or change Firecracker suspension admission; with no controller configured, nobody may.
func (g *Gateway) Methods() (auth.Methods, error) {
	callers := auth.Accounts(g.callerAuth, g.config.CallerAccounts)
	dispatcher := auth.Accounts(g.callerAuth, []auth.Account{g.config.DispatcherAccount})
	var controllers []auth.Account
	if g.config.SandboxControllerAccount != nil {
		controllers = []auth.Account{*g.config.SandboxControllerAccount}
	}
	executors := auth.Accounts(g.callerAuth, g.config.ExecutorCallers)
	controllerRule := auth.Accounts(g.callerAuth, controllers)
	var err error
	for _, entry := range []struct {
		rule     *auth.Rule
		services []string
	}{
		{&callers, g.config.RuntimeReaders}, {&dispatcher, g.config.SessionServices}, {&controllerRule, g.config.ControllerServices}, {&executors, g.config.ExecutorServices},
	} {
		*entry.rule, err = entry.rule.AllowServiceCallers(entry.services)
		if err != nil {
			return nil, err
		}
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
// configured. Existing Gateway deployments therefore keep the RPC absent by default.
func (g *Gateway) RegisterExecutor(server *grpc.Server) {
	if len(g.config.ExecutorCallers) == 0 && len(g.config.ExecutorServices) == 0 {
		return
	}
	executorpb.RegisterExecutorServer(server, &executorServer{gateway: g})
}
