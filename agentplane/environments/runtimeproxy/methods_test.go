package runtimeproxy

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"google.golang.org/grpc/credentials"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// registerStandIns registers servicers that answer Unimplemented to every call the policy admitted.
func registerStandIns(server *grpc.Server) {
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, environmentruntimepb.UnimplementedEnvironmentRuntimeServer{})
	harnesshostpb.RegisterHarnessHostServer(server, harnesshostpb.UnimplementedHarnessHostServer{})
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
	g := f.newServer(t, peertls.CentralSamplerURI)
	methods := serverMethods(t, g)
	connection := serveServer(t, g, registerStandIns)
	for _, desc := range []grpc.ServiceDesc{environmentruntimepb.EnvironmentRuntime_ServiceDesc, harnesshostpb.HarnessHost_ServiceDesc, executorpb.Executor_ServiceDesc} {
		for _, method := range grpcmethods.Of(desc) {
			want := codes.Unauthenticated
			if _, listed := methods[method.Name]; !listed {
				want = codes.PermissionDenied
			}
			for name, token := range map[string]string{
				"none": "", "app": f.appToken, "dispatcher": f.dispatcherToken,
				"unbound dispatcher": f.unboundDispatcherToken, "executor": f.executorToken,
				"controller": f.controllerToken, "workload": f.runtimeToken,
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
	g := f.newServer(t, peertls.CentralSamplerURI)
	connection := grpcmethods.Serve(t, serverMethods(t, g), func(server *grpc.Server) {
		executorpb.RegisterExecutorServer(server, &executorServer{server: g})
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
	g := f.newServer(t, peertls.CentralSamplerURI)
	g.config.RuntimeReaders = []string{peertls.AppURI, peertls.NotificationsURI}
	g.config.SessionServices = []string{peertls.SessionsURI}
	g.config.ControllerServices = []string{peertls.SandboxControllerURI}
	g.config.ExecutorServices = []string{peertls.CentralSamplerURI}
	ca := transporttesting.NewCA(t)
	serverTLS, err := ca.Issue(t, peertls.SessionDispatcherURI).WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(serverMethods(t, g).ServerOptions(0), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	registerStandIns(server)
	address := grpcserver.Serve(t, server)
	for _, actor := range []string{peertls.AppURI, peertls.NotificationsURI, peertls.SessionsURI, peertls.SandboxControllerURI, peertls.CentralSamplerURI, peertls.SessionDispatcherURI} {
		t.Run(actor, func(t *testing.T) {
			clientTLS, err := ca.Issue(t, actor).ClientTLSConfig(peertls.SessionDispatcherURI)
			if err != nil {
				t.Fatal(err)
			}
			conn, err := grpc.NewClient(address, grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			for _, desc := range []grpc.ServiceDesc{environmentruntimepb.EnvironmentRuntime_ServiceDesc, harnesshostpb.HarnessHost_ServiceDesc, executorpb.Executor_ServiceDesc} {
				for _, method := range grpcmethods.Of(desc) {
					allowed := false
					switch method.Name {
					case environmentruntimepb.EnvironmentRuntime_GetStatus_FullMethodName:
						allowed = actor == peertls.AppURI || actor == peertls.NotificationsURI
					case harnesshostpb.HarnessHost_ListSessions_FullMethodName:
						allowed = actor == peertls.AppURI || actor == peertls.SessionsURI || actor == peertls.NotificationsURI
					case harnesshostpb.HarnessHost_CreateSession_FullMethodName, harnesshostpb.HarnessHost_SubmitInput_FullMethodName, harnesshostpb.HarnessHost_ChangeModel_FullMethodName, harnesshostpb.HarnessHost_ChangeReasoningEffort_FullMethodName, harnesshostpb.HarnessHost_InterruptTurn_FullMethodName, harnesshostpb.HarnessHost_WatchSessions_FullMethodName:
						allowed = actor == peertls.SessionsURI
					case environmentruntimepb.EnvironmentRuntime_Quiesce_FullMethodName, environmentruntimepb.EnvironmentRuntime_PrepareSuspend_FullMethodName, environmentruntimepb.EnvironmentRuntime_ResumeAdmission_FullMethodName:
						allowed = actor == peertls.SandboxControllerURI
					default:
						if strings.HasPrefix(method.Name, "/"+executorpb.Executor_ServiceDesc.ServiceName+"/") {
							allowed = actor == peertls.CentralSamplerURI
						}
					}
					want := codes.PermissionDenied
					if allowed {
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

func TestHarnessHostListSessionsIncludesConfiguredReadersAndSessionCallers(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newServer(t)
	g.config.RuntimeReaders = []string{peertls.AppURI, peertls.SessionsURI}
	g.config.SessionServices = []string{peertls.SessionsURI}
	connection := serveServer(t, g, registerStandIns)
	var list grpcmethods.Method
	for _, method := range grpcmethods.Of(harnesshostpb.HarnessHost_ServiceDesc) {
		if method.Name == harnesshostpb.HarnessHost_ListSessions_FullMethodName {
			list = method
			break
		}
	}
	if list.Name == "" {
		t.Fatal("HarnessHost.ListSessions method not found")
	}
	for _, actor := range []string{peertls.AppURI, peertls.NotificationsURI, peertls.SessionsURI} {
		ctx, cancel := context.WithTimeout(context.WithValue(t.Context(), serviceConnectionKey{}, actor), 5*time.Second)
		err := list.Call(ctx, connection)
		cancel()
		want := codes.PermissionDenied
		if actor == peertls.AppURI || actor == peertls.SessionsURI {
			want = codes.Unimplemented
		}
		if status.Code(err) != want {
			t.Errorf("HarnessHost.ListSessions actor=%s: %v, want %s", actor, err, want)
		}
	}
}

func TestEmptyServiceGrantsAdmitNobody(t *testing.T) {
	f := newSandboxFixture(t)
	g := f.newServer(t)
	g.config.RuntimeReaders = nil
	g.config.SessionServices = nil
	g.config.ControllerServices = nil
	connection := serveServer(t, g, registerStandIns)
	for _, desc := range []grpc.ServiceDesc{environmentruntimepb.EnvironmentRuntime_ServiceDesc, harnesshostpb.HarnessHost_ServiceDesc, executorpb.Executor_ServiceDesc} {
		for _, method := range grpcmethods.Of(desc) {
			for _, actor := range []string{peertls.AppURI, peertls.NotificationsURI, peertls.SessionsURI, peertls.SandboxControllerURI, peertls.CentralSamplerURI} {
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
