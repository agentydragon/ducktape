package rpc

import (
	"maps"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// registerStandIns registers servicers that answer Unimplemented to every call the policy admitted.
func registerStandIns(server *grpc.Server) {
	notificationspb.RegisterNotificationsServer(server, notificationspb.UnimplementedNotificationsServer{})
}

func TestEveryRPCAdmitsOnlyItsCallers(t *testing.T) {
	resolver, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	// Every workload of the namespace may call Notifications for its own account, the operator included.
	workloads := map[string]codes.Code{
		"": codes.Unauthenticated, "test-token-unknown": codes.Unauthenticated, outsiderToken: codes.Unauthenticated,
		sandboxToken: codes.Unimplemented, otherSandboxToken: codes.Unimplemented, operatorToken: codes.Unimplemented,
	}
	for _, test := range []struct {
		name     string
		reader   string
		desc     grpc.ServiceDesc
		expected map[string]codes.Code
	}{
		{"Notifications", operatorName, notificationspb.Notifications_ServiceDesc, workloads},
	} {
		t.Run(test.name, func(t *testing.T) {
			methods := Methods(Options{Namespace: testNamespace, OperatorReader: test.reader}, resolver)
			connection := grpcmethods.Serve(t, methods, registerStandIns)
			for _, method := range grpcmethods.Of(test.desc) {
				t.Run(method.Name, func(t *testing.T) {
					for token, want := range test.expected {
						if got := status.Code(method.Call(bearing(t, token), connection)); got != want {
							t.Errorf("with token %q: %v, want %v", token, got, want)
						}
					}
					unlisted := maps.Clone(methods)
					delete(unlisted, method.Name)
					if got := status.Code(method.Call(bearing(t, operatorToken), grpcmethods.Serve(t, unlisted, registerStandIns))); got != codes.PermissionDenied {
						t.Errorf("unlisted, a caller got %v", got)
					}
				})
			}
		})
	}
}
