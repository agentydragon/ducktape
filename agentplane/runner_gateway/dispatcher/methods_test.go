package dispatcher

import (
	"context"
	"maps"
	"os"
	"testing"
	"time"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const methodsAudience = "test-dispatcher"

var (
	methodsAPI *kubeapiserver.Server
	// dispatcherCaller is the one account Methods admits; strangerToken proves another of its namespace.
	dispatcherCaller                     = auth.Account{Namespace: "test-callers", Name: "test-app"}
	dispatcherCallerToken, strangerToken string
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) (err error) {
		methodsAPI = server
		ctx := context.Background()
		if dispatcherCallerToken, err = server.WorkloadToken(ctx, dispatcherCaller, methodsAudience); err != nil {
			return err
		}
		strangerToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: dispatcherCaller.Namespace, Name: "test-stranger"}, methodsAudience)
		return err
	}))
}

func registerStandIn(server *grpc.Server) {
	dispatcherpb.RegisterSessionDispatcherServer(server, dispatcherpb.UnimplementedSessionDispatcherServer{})
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

func TestEveryRPCAdmitsOnlyTheDispatcherCallers(t *testing.T) {
	resolver, err := auth.NewResolver(methodsAPI.TokenReviews(), methodsAudience, []string{dispatcherCaller.Namespace})
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(resolver, []auth.Account{dispatcherCaller})
	connection := grpcmethods.Serve(t, methods, registerStandIn)
	for _, method := range grpcmethods.Of(dispatcherpb.SessionDispatcher_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			// The stand-in servicer answers Unimplemented to a call the policy admitted.
			for token, want := range map[string]codes.Code{
				"": codes.Unauthenticated, "test-token-unknown": codes.Unauthenticated,
				strangerToken: codes.PermissionDenied, dispatcherCallerToken: codes.Unimplemented,
			} {
				if got := status.Code(method.Call(bearing(t, token), connection)); got != want {
					t.Errorf("with token %q: %v, want %v", token, got, want)
				}
			}
			unlisted := maps.Clone(methods)
			delete(unlisted, method.Name)
			if got := status.Code(method.Call(bearing(t, dispatcherCallerToken), grpcmethods.Serve(t, unlisted, registerStandIn))); got != codes.PermissionDenied {
				t.Errorf("unlisted, an allowed caller got %v", got)
			}
		})
	}
}
