package dispatcher

import (
	"context"
	"maps"
	"net"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestDescribeDispatchMapsUnknownStoredStateToUnknown(t *testing.T) {
	dispatch, err := DescribeDispatch(&Dispatch{State: "FUTURE_STATE", Operation: "submit_input"})
	if err != nil {
		t.Fatalf("DescribeDispatch() error = %v", err)
	}
	if got := dispatch.GetState(); got != dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN {
		t.Fatalf("Dispatch state = %s, want unknown", got)
	}
}

func registerStandIn(server *grpc.Server) {
	dispatcherpb.RegisterSessionDispatcherServer(server, dispatcherpb.UnimplementedSessionDispatcherServer{})
}

func TestEveryRPCAdmitsOnlyConfiguredServiceSVIDs(t *testing.T) {
	rule, err := auth.Services([]string{peertls.AppURI, peertls.NotificationsURI})
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(rule)
	ca := transporttesting.NewCA(t)
	serverTLS, err := ca.Issue(t, peertls.SessionDispatcherURI).WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	serve := func(methods auth.Methods) string {
		t.Helper()
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		server := grpc.NewServer(append(methods.ServerOptions(0), grpc.Creds(credentials.NewTLS(serverTLS)))...)
		registerStandIn(server)
		go func() { _ = server.Serve(listener) }()
		t.Cleanup(server.Stop)
		return listener.Addr().String()
	}
	connect := func(address, uri string, presentCertificate bool) *grpc.ClientConn {
		t.Helper()
		config, err := ca.Issue(t, uri).ClientTLSConfig(peertls.SessionDispatcherURI)
		if err != nil {
			t.Fatal(err)
		}
		if !presentCertificate {
			config.Certificates = nil
			config.GetClientCertificate = nil
		}
		conn, err := grpc.NewClient(address, grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(config)))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = conn.Close() })
		return conn
	}
	address := serve(methods)
	app := connect(address, peertls.AppURI, true)
	notifications := connect(address, peertls.NotificationsURI, true)
	stranger := connect(address, peertls.ActionsURI, true)
	anonymous := connect(address, peertls.SessionDispatcherURI, false)
	for _, method := range grpcmethods.Of(dispatcherpb.SessionDispatcher_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			defer cancel()
			bearer := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer retired-token")
			// The stand-in answers Unimplemented only after admission succeeds.
			for _, test := range []struct {
				name string
				ctx  context.Context
				conn *grpc.ClientConn
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
			if got := status.Code(method.Call(ctx, connect(serve(unlisted), peertls.AppURI, true))); got != codes.PermissionDenied {
				t.Errorf("unlisted method, configured service got %v", got)
			}
		})
	}
}
