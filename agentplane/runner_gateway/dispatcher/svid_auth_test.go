package dispatcher

import (
	"context"
	"crypto/x509"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const workloadTestNamespace = "workload-test"

type rejectedTokenReviews struct{ t *testing.T }

func (r rejectedTokenReviews) Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	r.t.Error("SVID caller reached TokenReview")
	return &authenticationv1.TokenReview{}, nil
}

type svidAdmissionProbe struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	t         *testing.T
	principal auth.Principal
	calls     int
}

func (p *svidAdmissionProbe) check(ctx context.Context) error {
	p.t.Helper()
	caller, err := auth.AdmittedReviewedCaller(ctx)
	if err != nil {
		return err
	}
	if caller.ServiceAccount.GetAccount().GetNamespace() != p.principal.Account.Namespace ||
		caller.ServiceAccount.GetAccount().GetName() != p.principal.Account.Name ||
		caller.ServiceAccount.GetUid() != p.principal.ServiceAccountUID || caller.Pod == nil || *caller.Pod != p.principal {
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

func TestMethodsResolveSandboxSVIDBeforeNativeBearerAndAcrossWatch(t *testing.T) {
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
	svids, err := auth.NewSVIDResolver(workload.Objects, []string{workloadTestNamespace})
	if err != nil {
		t.Fatal(err)
	}
	tokens, err := auth.NewResolver(rejectedTokenReviews{t: t}, "backend-only", []string{workloadTestNamespace})
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(tokens.WithSVID(svids))

	ca := transporttesting.NewCA(t)
	serverCredential := ca.IssueWithUsage(t, environment_runtime_transport.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	serverTLS, err := serverCredential.WorkloadServerTLSConfig(environment_runtime_transport.SessionDispatcherURI)
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
		TLSConfig: serverTLS, ConnState: environment_runtime_transport.HTTPServerTLSExpiryConnState(nil),
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

	if err := workload.Objects.Delete(ctx, workload.Sandbox); err != nil {
		t.Fatal(err)
	}
	if _, err := client.ListSessions(ctx, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("deleted Sandbox SVID status = %v, want Unauthenticated", err)
	}
	if probe.calls != 2 {
		t.Fatalf("deleted Sandbox reached handler; calls = %d", probe.calls)
	}
}
