package dispatcher

import (
	"context"
	"maps"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func registerStandIn(server *grpc.Server) {
	dispatcherpb.RegisterSessionDispatcherServer(server, dispatcherpb.UnimplementedSessionDispatcherServer{})
}

func TestEveryRPCAdmitsOnlyConfiguredServiceSVIDs(t *testing.T) {
	rule, err := auth.Services([]string{peertls.AppURI, peertls.NotificationsURI})
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(rule)
	serve := func(methods auth.Methods) grpc.ClientConnInterface {
		t.Helper()
		return grpcmethods.ServeTLSWithTimeout(t, 0, peertls.SessionDispatcherURI, methods, registerStandIn,
			peertls.AppURI, peertls.NotificationsURI, peertls.ActionsURI)
	}
	connection := serve(methods)
	app := grpcmethods.AsCaller(connection, peertls.AppURI)
	notifications := grpcmethods.AsCaller(connection, peertls.NotificationsURI)
	stranger := grpcmethods.AsCaller(connection, peertls.ActionsURI)
	anonymous := grpcmethods.AsCaller(connection, "")
	for _, method := range grpcmethods.Of(dispatcherpb.SessionDispatcher_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			defer cancel()
			bearer := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer retired-token")
			// The stand-in answers Unimplemented only after admission succeeds.
			for _, test := range []struct {
				name string
				ctx  context.Context
				conn grpc.ClientConnInterface
				want codes.Code
			}{
				{"app", ctx, app, codes.Unimplemented},
				{"notifications", ctx, notifications, codes.Unimplemented},
				{"unlisted service", ctx, stranger, codes.PermissionDenied},
				{"no certificate", ctx, anonymous, codes.Unauthenticated},
				{"bearer only", bearer, anonymous, codes.Unauthenticated},
				{"certificate and bearer", bearer, app, codes.Unauthenticated},
			} {
				if got := status.Code(method.Call(test.ctx, test.conn)); got != test.want {
					t.Errorf("%s: %v, want %v", test.name, got, test.want)
				}
			}
			unlisted := maps.Clone(methods)
			delete(unlisted, method.Name)
			if got := status.Code(method.Call(ctx, grpcmethods.AsCaller(serve(unlisted), peertls.AppURI))); got != codes.PermissionDenied {
				t.Errorf("unlisted method, configured service got %v", got)
			}
		})
	}
}
