package runtimeproxy

import (
	"context"
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

func TestEveryRPCRefusesLegacyBearerCallers(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, peertls.CentralSamplerURI)
	methods := gatewayMethods(t, g)
	connection := serveGateway(t, g, registerStandIns)
	for _, desc := range []grpc.ServiceDesc{environmentruntimepb.EnvironmentRuntime_ServiceDesc, executorpb.Executor_ServiceDesc} {
		for _, method := range grpcmethods.Of(desc) {
			want := codes.Unauthenticated
			if _, listed := methods[method.Name]; !listed {
				want = codes.PermissionDenied
			}
			for name, token := range map[string]string{
				"none": "", "app": f.appToken, "dispatcher": f.dispatcherToken,
				"unbound dispatcher": f.unboundDispatcherToken, "executor": f.executorToken,
				"controller": f.controllerToken, "workload": f.runnerToken,
			} {
				if got := status.Code(method.Call(bearing(t, token), connection)); got != want {
					t.Errorf("%s %s: %v, want %v", method.Name, name, got, want)
				}
			}
		}
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
	g := f.newGateway(t, peertls.CentralSamplerURI)
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

func TestRuntimeProxyServiceRolesStayMethodScoped(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t, peertls.CentralSamplerURI)
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

func TestEmptyServiceGrantsAdmitNobody(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newGateway(t)
	g.config.RuntimeReaders = nil
	g.config.SessionServices = nil
	g.config.ControllerServices = nil
	connection := serveGateway(t, g, registerStandIns)
	for _, desc := range []grpc.ServiceDesc{environmentruntimepb.EnvironmentRuntime_ServiceDesc, executorpb.Executor_ServiceDesc} {
		for _, method := range grpcmethods.Of(desc) {
			for _, actor := range []string{peertls.AppURI, peertls.SessionsURI, peertls.SandboxControllerURI, peertls.CentralSamplerURI} {
				ctx, cancel := context.WithTimeout(context.WithValue(t.Context(), serviceConnectionKey{}, actor), 5*time.Second)
				err := method.Call(ctx, connection)
				cancel()
				if status.Code(err) != codes.PermissionDenied {
					t.Errorf("%s %s = %v, want PermissionDenied", method.Name, actor, err)
				}
			}
		}
	}
}
