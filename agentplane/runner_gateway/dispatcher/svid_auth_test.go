package dispatcher

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const workloadTestNamespace = "workload-test"

type svidAdmissionProbe struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	t         *testing.T
	principal auth.Principal
	calls     int
}

func (p *svidAdmissionProbe) check(ctx context.Context) error {
	p.t.Helper()
	caller, err := auth.Caller(ctx)
	if err != nil {
		return err
	}
	if caller != p.principal {
		p.t.Errorf("admitted caller = %+v, want Sandbox principal %+v", caller, p.principal)
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
	api, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := api.Stop(); err != nil {
			t.Error(err)
		}
	})
	workload := workloadauthtesting.NewWorkload(t, api, workloadTestNamespace, "sandbox-a")
	svids, err := auth.NewSVIDResolver(workload.Objects, []string{workloadTestNamespace}, nil)
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(auth.AnyAccount(svids))

	ca := transporttesting.NewCA(t)
	serverCredential := ca.IssueWithUsage(t, environment_runtime_transport.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := serverCredential.WorkloadServerTLSConfig(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	connState, err := environment_runtime_transport.ConfigureHTTPServerTLSExpiry(serverTLS)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.NextProtos = []string{"h2", "http/1.1"}
	clientCredential := ca.Issue(t, workload.URI)
	clientTLS, err := clientCredential.ClientTLSConfig(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	probe := &svidAdmissionProbe{t: t, principal: workload.Principal}
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
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs(
		"authorization", "Bearer forged-backend-token",
		"x-agentplane-subject", "attacker/forged",
	))
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

type countingTokenReviews struct {
	delegate auth.TokenReviews
	calls    atomic.Int32
}

func (r *countingTokenReviews) Create(ctx context.Context, review *authenticationv1.TokenReview, options metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	r.calls.Add(1)
	return r.delegate.Create(ctx, review, options)
}

type mixedAdmissionProbe struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	t        *testing.T
	expected map[auth.Account]auth.Principal
	calls    atomic.Int32
}

func (p *mixedAdmissionProbe) ListSessions(ctx context.Context, _ *dispatcherpb.ListSessionsRequest) (*dispatcherpb.ListSessionsResponse, error) {
	p.t.Helper()
	caller, err := auth.AdmittedReviewedCaller(ctx)
	if err != nil {
		return nil, err
	}
	if caller.ServiceAccount == nil || caller.ServiceAccount.GetAccount() == nil || caller.Pod == nil {
		p.t.Errorf("admitted caller lacks account or Pod proof: %+v", caller)
		return nil, status.Error(codes.PermissionDenied, "caller proof is incomplete")
	}
	account := auth.Account{
		Namespace: caller.ServiceAccount.GetAccount().GetNamespace(),
		Name:      caller.ServiceAccount.GetAccount().GetName(),
	}
	expected, ok := p.expected[account]
	if !ok || caller.ServiceAccount.GetUid() != expected.ServiceAccountUID || *caller.Pod != expected {
		p.t.Errorf("admitted caller = %+v, want account proof %+v", caller, expected)
		return nil, status.Error(codes.PermissionDenied, "unexpected caller")
	}
	p.calls.Add(1)
	return &dispatcherpb.ListSessionsResponse{}, nil
}

func TestMixedWorkloadListenerSelectsExactlyOneCredentialBeforeDispatcherMethods(t *testing.T) {
	api, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := api.Stop(); err != nil {
			t.Error(err)
		}
	})
	workload := workloadauthtesting.NewWorkload(t, api, workloadTestNamespace, "sandbox-a")
	svids, err := auth.NewSVIDResolver(workload.Objects, []string{workloadTestNamespace}, nil)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	bearerAccount := auth.Account{Namespace: workloadTestNamespace, Name: "workload-bearer"}
	serviceAccount, err := api.ServiceAccount(ctx, bearerAccount)
	if err != nil {
		t.Fatal(err)
	}
	pod, err := api.Pod(ctx, bearerAccount)
	if err != nil {
		t.Fatal(err)
	}
	const workloadAudience = "test-runner-workload"
	bearerToken, err := api.PodToken(ctx, pod, workloadAudience)
	if err != nil {
		t.Fatal(err)
	}
	bearerPrincipal := auth.Principal{
		Account: bearerAccount, ServiceAccountUID: string(serviceAccount.UID), PodName: pod.Name, PodUID: string(pod.UID),
	}
	reviews := &countingTokenReviews{delegate: api.TokenReviews()}
	tokens, err := auth.NewResolver(reviews, workloadAudience, []string{workloadTestNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	ca := transporttesting.NewCA(t)
	serverCredential := ca.IssueWithUsage(t, environment_runtime_transport.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	listenerConfig := listener.Config{
		ListenAddress: ":8080",
		TLS:           &listener.TLS{CertFile: serverCredential.CertFile, KeyFile: serverCredential.KeyFile},
		Auth: []listener.Authentication{
			&listener.SPIFFEAuth{CAFile: ca.RootFile},
			&listener.KubernetesBearerAuth{Audiences: []string{workloadAudience}},
		},
	}
	serverTLS, err := listenerConfig.TLSConfig(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.NextProtos = []string{"h2"}
	workloadAuth, err := tokens.WithListenerAuthentication(listenerConfig, svids)
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(auth.ReviewedServiceAccount(workloadAuth))
	probe := &mixedAdmissionProbe{
		t: t,
		expected: map[auth.Account]auth.Principal{
			workload.Principal.Account: workload.Principal,
			bearerAccount:              bearerPrincipal,
		},
	}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, probe)
	netListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(netListener) }()
	t.Cleanup(server.Stop)

	validClient := ca.Issue(t, workload.URI)
	validClientTLS, err := validClient.ClientTLSConfig(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	validClientConn, err := grpc.NewClient(netListener.Addr().String(), grpc.WithTransportCredentials(credentials.NewTLS(validClientTLS)), grpc.WithNoProxy())
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
	bearerConn, err := grpc.NewClient(netListener.Addr().String(), grpc.WithTransportCredentials(credentials.NewTLS(noCertificateTLS)), grpc.WithNoProxy())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = bearerConn.Close() })
	bearerContext := metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer "+bearerToken))
	if _, err := dispatcherpb.NewSessionDispatcherClient(bearerConn).ListSessions(bearerContext, &dispatcherpb.ListSessionsRequest{}); err != nil {
		t.Fatalf("TLS bearer call = %v", err)
	}
	if reviews.calls.Load() != 1 {
		t.Fatalf("TokenReview calls after direct bearer = %d, want 1", reviews.calls.Load())
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
	invalidClientTLS, err := invalidClient.ClientTLSConfig(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	invalidClientConn, err := grpc.NewClient(netListener.Addr().String(), grpc.WithTransportCredentials(credentials.NewTLS(invalidClientTLS)), grpc.WithNoProxy())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = invalidClientConn.Close() })
	if _, err := dispatcherpb.NewSessionDispatcherClient(invalidClientConn).ListSessions(ctx, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("invalid SVID without bearer = %v, want Unauthenticated", err)
	}
	if _, err := dispatcherpb.NewSessionDispatcherClient(invalidClientConn).ListSessions(bearerContext, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("invalid SVID with valid bearer = %v, want Unauthenticated", err)
	}
	if probe.calls.Load() != 2 || reviews.calls.Load() != 1 {
		t.Fatalf("handler calls = %d, TokenReview calls = %d; want 2 and 1", probe.calls.Load(), reviews.calls.Load())
	}

	spiffeOnly := listenerConfig
	spiffeOnly.Auth = []listener.Authentication{&listener.SPIFFEAuth{CAFile: ca.RootFile}}
	disabledBearerAuth, err := tokens.WithListenerAuthentication(spiffeOnly, svids)
	if err != nil {
		t.Fatal(err)
	}
	noCertificatePeer := peer.NewContext(metadata.NewIncomingContext(ctx, metadata.Pairs("authorization", "Bearer "+bearerToken)), &peer.Peer{
		AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{}},
	})
	if _, err := disabledBearerAuth.ResolveReviewedCaller(noCertificatePeer); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("bearer with no certificate auth mode enabled = %v, want Unauthenticated", err)
	}
	if reviews.calls.Load() != 1 {
		t.Fatalf("disabled bearer reached TokenReview; calls = %d", reviews.calls.Load())
	}
}
