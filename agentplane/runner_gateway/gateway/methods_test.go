package gateway

import (
	"context"
	"maps"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// registerStandIns registers servicers that answer Unimplemented to every call the policy admitted.
func registerStandIns(server *grpc.Server) {
	runnerservicepb.RegisterRunnerServer(server, runnerservicepb.UnimplementedRunnerServer{})
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
			runnerservicepb.Runner_Quiesce_FullMethodName: "controller",
		}},
		{"without a Sandbox controller", withoutController, map[string]string{
			runnerservicepb.Runner_Quiesce_FullMethodName: "",
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			methods := test.gateway.Methods()
			connection := grpcmethods.Serve(t, methods, registerStandIns)
			for _, desc := range []struct {
				service grpc.ServiceDesc
				caller  string
			}{{runnerservicepb.Runner_ServiceDesc, "app"}, {executorpb.Executor_ServiceDesc, "executor caller"}} {
				for _, method := range grpcmethods.Of(desc.service) {
					admitted, special := test.admitted[method.Name]
					if !special {
						admitted = desc.caller
					}
					t.Run(method.Name, func(t *testing.T) {
						if method.Name == runnerservicepb.Runner_WatchState_FullMethodName {
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
							case name == "runner":
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
