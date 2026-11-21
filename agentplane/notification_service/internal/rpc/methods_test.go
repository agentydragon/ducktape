package rpc

import (
	"maps"
	"slices"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
)

func TestEveryRPCAdmitsOnlyItsCallers(t *testing.T) {
	fixture := newServer(t)
	svids, err := auth.NewSVIDResolver(fixture.workload.Objects, []string{testNamespace}, &resolver.Identities{PolicyNamespace: testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	methods, err := Methods(Options{OperatorReaderServices: []string{peertls.AppURI}}, svids)
	if err != nil {
		t.Fatal(err)
	}
	serving := fixture.ca.Issue(t, peertls.NotificationsURI)
	serverTLS, err := serving.WorkloadServerTLSConfigWithServices(peertls.NotificationsURI, true)
	if err != nil {
		t.Fatal(err)
	}
	serve := func(methods auth.Methods) *routedClientConn {
		server := grpc.NewServer(append(methods.ServerOptions(callDeadline), grpc.Creds(credentials.NewTLS(serverTLS)))...)
		notificationspb.RegisterNotificationsServer(server, notificationspb.UnimplementedNotificationsServer{})
		address := grpcserver.Serve(t, server)
		connect := func(config *peertls.ClientTLS) *grpc.ClientConn {
			security, err := config.Credentials()
			if err != nil {
				t.Fatal(err)
			}
			connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(security))
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = connection.Close() })
			return connection
		}
		service := fixture.ca.Issue(t, peertls.AppURI)
		serviceTLS := &peertls.ClientTLS{CAFile: service.CAFile, ServerURI: peertls.NotificationsURI, CertFile: service.CertFile, KeyFile: service.KeyFile, ClientURI: peertls.AppURI}
		identity := connectIdentityClient(t, fixture.ca, fixture.workload.Workload, address)
		other := connectIdentityClient(t, fixture.ca, fixture.otherWorkload.Workload, address)
		t.Cleanup(func() { _ = identity.Close(); _ = other.Close() })
		return &routedClientConn{identity: identity, other: other, operator: connect(serviceTLS), unauthenticated: connect(&peertls.ClientTLS{CAFile: serving.CAFile, ServerURI: peertls.NotificationsURI})}
	}
	connection := serve(methods)
	for _, method := range grpcmethods.Of(notificationspb.Notifications_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			for _, caller := range []string{identityCaller, otherIdentityCaller, operatorCaller, "", sandboxToken, otherSandboxToken, operatorToken, outsiderToken, "unknown"} {
				want := codes.Unauthenticated
				switch caller {
				case identityCaller, otherIdentityCaller:
					want = codes.Unimplemented
				case operatorCaller:
					want = codes.PermissionDenied
					if slices.Contains(operatorReaderMethods(), method.Name) {
						want = codes.Unimplemented
					}
				}
				if got := status.Code(method.Call(bearing(t, caller), connection)); got != want {
					t.Errorf("caller %q got %v, want %v", caller, got, want)
				}
			}
			unlisted := maps.Clone(methods)
			delete(unlisted, method.Name)
			if got := status.Code(method.Call(bearing(t, identityCaller), serve(unlisted))); got != codes.PermissionDenied {
				t.Errorf("unlisted method got %v", got)
			}
		})
	}
}
