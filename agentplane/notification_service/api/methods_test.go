package api

import (
	"maps"
	"testing"

	operatorpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// registerStandIns registers servicers that answer Unimplemented to every call the policy admitted.
func registerStandIns(server *grpc.Server) {
	notificationspb.RegisterNotificationsServer(server, notificationspb.UnimplementedNotificationsServer{})
	operatorpb.RegisterNotificationsOperatorServer(server, operatorpb.UnimplementedNotificationsOperatorServer{})
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
	operator := map[string]codes.Code{
		"": codes.Unauthenticated, "test-token-unknown": codes.Unauthenticated, outsiderToken: codes.Unauthenticated,
		sandboxToken: codes.PermissionDenied, otherSandboxToken: codes.PermissionDenied, operatorToken: codes.Unimplemented,
	}
	noOperator := maps.Clone(operator)
	noOperator[operatorToken] = codes.PermissionDenied
	for _, test := range []struct {
		name     string
		reader   string
		desc     grpc.ServiceDesc
		expected map[string]codes.Code
	}{
		{"Notifications", operatorName, notificationspb.Notifications_ServiceDesc, workloads},
		{"NotificationsOperator", operatorName, operatorpb.NotificationsOperator_ServiceDesc, operator},
		{"NotificationsOperator without an operator", "", operatorpb.NotificationsOperator_ServiceDesc, noOperator},
	} {
		t.Run(test.name, func(t *testing.T) {
			methods := Methods(Options{Resolver: resolver, Namespace: testNamespace, OperatorReader: test.reader})
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
