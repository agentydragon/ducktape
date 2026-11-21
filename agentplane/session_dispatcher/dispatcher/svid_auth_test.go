package dispatcher

import (
	"context"
	"crypto/x509"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const workloadTestNamespace = "workload-test"

type svidAdmissionProbe struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	t        *testing.T
	identity auth.IdentityPrincipal
	calls    int
}

func (p *svidAdmissionProbe) check(ctx context.Context) error {
	p.t.Helper()
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return err
	}
	identity, ok := admission.(auth.IdentityAdmission)
	if !ok || identity.Caller != p.identity {
		p.t.Errorf("admitted caller = %+v, want Identity subject %+v", admission, p.identity)
	}
	p.calls++
	return nil
}

func (p *svidAdmissionProbe) ListSessions(ctx context.Context, _ *dispatcherpb.ListSessionsRequest) (*dispatcherpb.ListSessionsResponse, error) {
	if err := p.check(ctx); err != nil {
		return nil, err
	}
	return &dispatcherpb.ListSessionsResponse{}, nil
}

func (p *svidAdmissionProbe) WatchDispatches(
	_ *dispatcherpb.WatchDispatchesRequest,
	stream grpc.ServerStreamingServer[dispatcherpb.WatchDispatchesResponse],
) error {
	if err := auth.RevalidateStream(stream.Context()); err != nil {
		return err
	}
	if err := p.check(stream.Context()); err != nil {
		return err
	}
	return stream.Send(&dispatcherpb.WatchDispatchesResponse{
		Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}},
	})
}

func TestMethodsResolveSandboxSVIDAndRevalidateStream(t *testing.T) {
	api := kubeapiserver.StartForTest(t)
	workload := workloadauthtesting.NewIdentityWorkload(t, api, workloadTestNamespace, "sandbox-a", workloadTestNamespace).Workload
	svids, err := auth.NewSVIDResolver(workload.Objects, []string{workloadTestNamespace}, &resolver.Identities{PolicyNamespace: workloadTestNamespace})
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(auth.AnySubject(svids))

	ca := transporttesting.NewCA(t)
	serverCredential := ca.IssueWithUsage(t, peertls.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := serverCredential.WorkloadServerTLSConfig(peertls.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	connState, err := peertls.ConfigureHTTPServerTLSExpiry(serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.NextProtos = []string{"h2", "http/1.1"}
	clientCredential := ca.Issue(t, workload.URI)
	clientTLS, err := clientCredential.ClientTLSConfig(peertls.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	probe := &svidAdmissionProbe{t: t, identity: workload.Subject}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, probe)
	httpServer := &http.Server{
		Handler: http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
			server.ServeHTTP(writer, request)
		}),
		TLSConfig: serverTLS, ConnState: connState,
	}
	go func() { _ = httpServer.ServeTLS(listener, "", "") }()
	t.Cleanup(func() {
		_ = httpServer.Close()
		server.Stop()
	})

	connection, err := grpc.NewClient(
		listener.Addr().String(),
		grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)),
		grpc.WithNoProxy(),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	client := dispatcherpb.NewSessionDispatcherClient(connection)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	mixed := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer forged-backend-token")
	if _, err := client.ListSessions(mixed, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("certificate plus bearer = %v, want Unauthenticated", err)
	}
	if probe.calls != 0 {
		t.Fatalf("mixed credentials reached handler %d times", probe.calls)
	}
	ctx = metadata.AppendToOutgoingContext(ctx, "x-agentplane-subject", "attacker/forged")
	if _, err := client.ListSessions(ctx, &dispatcherpb.ListSessionsRequest{}); err != nil {
		t.Fatal(err)
	}
	watch, err := client.WatchDispatches(ctx, &dispatcherpb.WatchDispatchesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := watch.Recv(); err != nil {
		t.Fatal(err)
	}
	if probe.calls != 2 {
		t.Fatalf("handler calls = %d, want 2", probe.calls)
	}

	workload.Delete(t)
	if _, err := client.ListSessions(ctx, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("deleted Sandbox SVID status = %v, want Unauthenticated", err)
	}
	if probe.calls != 2 {
		t.Fatalf("deleted Sandbox reached handler; calls = %d", probe.calls)
	}
}

func TestSVIDListenerNeverAcceptsBearerOrDelegationWithoutItsOwnGrant(t *testing.T) {
	api := kubeapiserver.StartForTest(t)
	workload := workloadauthtesting.NewIdentityWorkload(t, api, workloadTestNamespace, "sandbox-a", workloadTestNamespace).Workload
	svids, err := auth.NewSVIDResolver(workload.Objects, []string{workloadTestNamespace}, &resolver.Identities{PolicyNamespace: workloadTestNamespace})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	const bearerToken = "retired-workload-token"
	ca := transporttesting.NewCA(t)
	serverCredential := ca.IssueWithUsage(t, peertls.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	listenerConfig := listener.Config{
		ListenAddress: ":8080",
		TLS:           &listener.TLS{CertFile: serverCredential.CertFile, KeyFile: serverCredential.KeyFile},
		Auth: []listener.Authentication{
			&listener.SPIFFEAuth{CAFile: ca.RootFile},
		},
	}
	serverCredentials, err := listenerConfig.ServerCredentials(peertls.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(auth.AnySubject(svids))
	probe := &svidAdmissionProbe{t: t, identity: workload.Subject}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(serverCredentials))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, probe)
	serverAddr := grpcserver.Serve(t, server)

	validClient := ca.Issue(t, workload.URI)
	validClientTLS, err := validClient.ClientTLSConfig(peertls.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	validClientConn, err := grpc.NewClient(serverAddr, grpc.WithTransportCredentials(credentials.NewTLS(validClientTLS)), grpc.WithNoProxy())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = validClientConn.Close() })
	client := dispatcherpb.NewSessionDispatcherClient(validClientConn)
	if _, err := client.ListSessions(ctx, &dispatcherpb.ListSessionsRequest{}); err != nil {
		t.Fatalf("Sandbox SVID call = %v", err)
	}

	noCertificateTLS := validClientTLS.Clone()
	noCertificateTLS.Certificates = nil
	noCertificateTLS.GetClientCertificate = nil
	bearerConn, err := grpc.NewClient(serverAddr, grpc.WithTransportCredentials(credentials.NewTLS(noCertificateTLS)), grpc.WithNoProxy())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = bearerConn.Close() })
	bearerContext := metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer "+bearerToken))
	if _, err := dispatcherpb.NewSessionDispatcherClient(bearerConn).ListSessions(bearerContext, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("bearer without certificate = %v, want Unauthenticated", err)
	}

	if _, err := client.ListSessions(bearerContext, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("certificate and bearer call = %v, want Unauthenticated", err)
	}
	delegationContext := metadata.NewOutgoingContext(ctx, metadata.Pairs(auth.DelegationMetadataKey, "delegation-envelope"))
	if _, err := client.ListSessions(delegationContext, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("certificate and delegation call = %v, want Unauthenticated", err)
	}
	invalidRuntimeURI := "spiffe://agentplane/" + workloadTestNamespace + "/missing-sandbox/credential"
	invalidClient := ca.Issue(t, invalidRuntimeURI)
	invalidClientTLS, err := invalidClient.ClientTLSConfig(peertls.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	invalidClientConn, err := grpc.NewClient(serverAddr, grpc.WithTransportCredentials(credentials.NewTLS(invalidClientTLS)), grpc.WithNoProxy())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = invalidClientConn.Close() })
	if _, err := dispatcherpb.NewSessionDispatcherClient(invalidClientConn).ListSessions(ctx, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("invalid SVID without bearer = %v, want Unauthenticated", err)
	}
	if _, err := dispatcherpb.NewSessionDispatcherClient(invalidClientConn).ListSessions(bearerContext, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("invalid SVID with retired bearer = %v, want Unauthenticated", err)
	}
	if probe.calls != 1 {
		t.Fatalf("handler calls = %d, want only the valid SVID call", probe.calls)
	}
}
