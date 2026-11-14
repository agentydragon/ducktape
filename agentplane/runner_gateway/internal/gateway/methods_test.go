package gateway

import (
	"context"
	"maps"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"google.golang.org/grpc/credentials"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// registerStandIns registers servicers that answer Unimplemented to every call the policy admitted.
func registerStandIns(server *grpc.Server) {
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, environmentruntimepb.UnimplementedEnvironmentRuntimeServer{})
	executorpb.RegisterExecutorServer(server, executorpb.UnimplementedExecutorServer{})
}

func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	t.Cleanup(cancel)
	if token == "" {
		return ctx
	}
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

func TestEveryRPCAdmitsOnlyItsCallers(t *testing.T) {
	f := newSandboxFixture(t)
	withController := f.newGateway(t, "test-runner-gateway", "192.0.2.30", executorAccount)
	withoutController := f.newGateway(t, "test-runner-gateway", "192.0.2.30", executorAccount)
	withoutController.config.SandboxControllerAccount = nil
	tokens := map[string]string{
		"dispatcher": f.dispatcherToken, "unbound dispatcher": f.unboundDispatcherToken,
		"app": f.appToken, "executor caller": f.executorToken, "controller": f.controllerToken,
		// The runner's namespace is not one the callers' resolver serves.
		"runner": f.runnerToken,
	}
	for _, test := range []struct {
		name    string
		gateway *Gateway
		// admitted is who may call each method; a method absent from it is unlisted.
		admitted map[string]string
	}{
		{"with a Sandbox controller", withController, map[string]string{
			environmentruntimepb.EnvironmentRuntime_Quiesce_FullMethodName:         "controller",
			environmentruntimepb.EnvironmentRuntime_PrepareSuspend_FullMethodName:  "controller",
			environmentruntimepb.EnvironmentRuntime_ResumeAdmission_FullMethodName: "controller",
		}},
		{"without a Sandbox controller", withoutController, map[string]string{
			environmentruntimepb.EnvironmentRuntime_Quiesce_FullMethodName:         "",
			environmentruntimepb.EnvironmentRuntime_PrepareSuspend_FullMethodName:  "",
			environmentruntimepb.EnvironmentRuntime_ResumeAdmission_FullMethodName: "",
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			methods := gatewayMethods(t, test.gateway)
			connection := grpcmethods.Serve(t, methods, registerStandIns)
			for _, desc := range []struct {
				service grpc.ServiceDesc
				caller  string
			}{{environmentruntimepb.EnvironmentRuntime_ServiceDesc, "app"}, {executorpb.Executor_ServiceDesc, "executor caller"}} {
				for _, method := range grpcmethods.Of(desc.service) {
					admitted, special := test.admitted[method.Name]
					if !special {
						admitted = desc.caller
						switch method.Name {
						case environmentruntimepb.EnvironmentRuntime_CreateSession_FullMethodName,
							environmentruntimepb.EnvironmentRuntime_SubmitInput_FullMethodName,
							environmentruntimepb.EnvironmentRuntime_ChangeModel_FullMethodName,
							environmentruntimepb.EnvironmentRuntime_ChangeReasoningEffort_FullMethodName,
							environmentruntimepb.EnvironmentRuntime_InterruptTurn_FullMethodName:
							admitted = "dispatcher"
						}
					}
					t.Run(method.Name, func(t *testing.T) {
						if method.Name == environmentruntimepb.EnvironmentRuntime_WatchState_FullMethodName {
							// The proxy does not relay WatchState, so nobody may call it.
							for name, token := range tokens {
								if got := status.Code(method.Call(bearing(t, token), connection)); got != codes.PermissionDenied {
									t.Errorf("%s: %v, want PermissionDenied", name, got)
								}
							}
							return
						}
						if got := status.Code(method.Call(bearing(t, ""), connection)); got != codes.Unauthenticated {
							t.Errorf("no bearer: %v", got)
						}
						for name, token := range tokens {
							want := codes.PermissionDenied
							switch {
							case name == "runner" || name == "unbound dispatcher":
								want = codes.Unauthenticated
							case name == admitted:
								want = codes.Unimplemented
							}
							if got := status.Code(method.Call(bearing(t, token), connection)); got != want {
								t.Errorf("%s: %v, want %v", name, got, want)
							}
						}
						if admitted == "" {
							return
						}
						unlisted := maps.Clone(methods)
						delete(unlisted, method.Name)
						if got := status.Code(method.Call(bearing(t, tokens[admitted]), grpcmethods.Serve(t, unlisted, registerStandIns))); got != codes.PermissionDenied {
							t.Errorf("unlisted, %s got %v", admitted, got)
						}
					})
				}
			}
		})
	}
}

func TestUnauthorizedExecutorCallerCannotReadEnvironmentFailureState(t *testing.T) {
	f := newSandboxFixture(t)
	sandbox := f.sandbox.DeepCopy()
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	sandbox.Status.Incarnation = nil
	if err := f.objects.Status().Update(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
	g := f.newGateway(t, "test-runner-gateway", "192.0.2.30", executorAccount)
	connection := grpcmethods.Serve(t, gatewayMethods(t, g), func(server *grpc.Server) {
		executorpb.RegisterExecutorServer(server, &executorServer{gateway: g})
	})
	stream, err := executorpb.NewExecutorClient(connection).OpenSession(bearing(t, ""), &executorpb.OpenSessionRequest{
		Scope: &executorpb.ExecutionScope{
			Destination: &identitypb.SandboxDestination{
				Target: &identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: string(f.sandbox.UID)}, Owner: &identitypb.ServiceAccount{Namespace: f.namespace, Name: f.sandbox.Name},
			},
			ExpectedIncarnationUid: string(f.pod.UID), SessionId: "sampler-session",
		},
	})
	if err == nil {
		_, err = stream.Recv()
	}
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unauthorized executor call error=%v, want Unauthenticated", err)
	}
	for _, detail := range status.Convert(err).Details() {
		if _, ok := detail.(*executorpb.ExecutionEnvironmentFailure); ok {
			t.Fatalf("unauthorized executor call disclosed the environment state: %v", detail)
		}
	}
}

func TestCheckpointedLauncherWithIncarnationRoutesAsSuspended(t *testing.T) {
	sandbox := &sandboxapi.Sandbox{}
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	sandbox.Status.StopStage = sandboxapi.StopStageStopped
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "launcher", UID: "pod-uid"}
	if phase := sandboxPhase(sandbox); phase != modeSuspended {
		t.Fatalf("checkpointed launcher phase = %s, want Suspended", phase)
	}
}

func gatewayMethods(t *testing.T, g *Gateway) auth.Methods {
	t.Helper()
	methods, err := g.Methods()
	if err != nil {
		t.Fatal(err)
	}
	return methods
}

func TestRuntimeProxyServiceRolesStayMethodScoped(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, "test-service-proxy", "192.0.2.44", executorAccount)
	g.config.RuntimeReaders = []string{peertls.AppURI}
	g.config.SessionServices = []string{peertls.SessionsURI}
	g.config.ControllerServices = []string{peertls.SandboxControllerURI}
	g.config.ExecutorServices = []string{peertls.CentralSamplerURI}
	ca := transporttesting.NewCA(t)
	serverTLS, err := ca.Issue(t, peertls.SessionDispatcherURI).WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(gatewayMethods(t, g).ServerOptions(0), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	registerStandIns(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	for _, actor := range []string{peertls.AppURI, peertls.SessionsURI, peertls.SandboxControllerURI, peertls.CentralSamplerURI, peertls.SessionDispatcherURI} {
		t.Run(actor, func(t *testing.T) {
			clientTLS, err := ca.Issue(t, actor).ClientTLSConfig(peertls.SessionDispatcherURI)
			if err != nil {
				t.Fatal(err)
			}
			conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			for _, desc := range []grpc.ServiceDesc{environmentruntimepb.EnvironmentRuntime_ServiceDesc, executorpb.Executor_ServiceDesc} {
				for _, method := range grpcmethods.Of(desc) {
					allowed := ""
					switch method.Name {
					case environmentruntimepb.EnvironmentRuntime_ListSessions_FullMethodName, environmentruntimepb.EnvironmentRuntime_GetStatus_FullMethodName:
						allowed = peertls.AppURI
					case environmentruntimepb.EnvironmentRuntime_CreateSession_FullMethodName, environmentruntimepb.EnvironmentRuntime_SubmitInput_FullMethodName, environmentruntimepb.EnvironmentRuntime_ChangeModel_FullMethodName, environmentruntimepb.EnvironmentRuntime_ChangeReasoningEffort_FullMethodName, environmentruntimepb.EnvironmentRuntime_InterruptTurn_FullMethodName:
						allowed = peertls.SessionsURI
					case environmentruntimepb.EnvironmentRuntime_Quiesce_FullMethodName, environmentruntimepb.EnvironmentRuntime_PrepareSuspend_FullMethodName, environmentruntimepb.EnvironmentRuntime_ResumeAdmission_FullMethodName:
						allowed = peertls.SandboxControllerURI
					default:
						if strings.HasPrefix(method.Name, "/"+executorpb.Executor_ServiceDesc.ServiceName+"/") {
							allowed = peertls.CentralSamplerURI
						}
					}
					want := codes.PermissionDenied
					if allowed == actor {
						want = codes.Unimplemented
					}
					ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
					err = method.Call(ctx, conn)
					cancel()
					if status.Code(err) != want {
						t.Fatalf("%s actor=%s: %v,want=%s", method.Name, actor, err, want)
					}
				}
			}
		})
	}
}
