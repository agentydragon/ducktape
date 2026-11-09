package ingress

import (
	"net"
	"sync/atomic"
	"testing"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestSVIDKeepsTheFirstWriterServiceAccountGate(t *testing.T) {
	namespace := "history-svid-" + uuid.NewString()
	first := workloadauthtesting.NewWorkload(t, kube, namespace, "first-writer")
	other := workloadauthtesting.NewWorkload(t, kube, namespace, "other-writer")
	ca := transporttesting.NewCA(t)
	svids, err := auth.NewKubernetesSVIDResolver(kube.Config, []string{namespace})
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{namespace})
	if err != nil {
		t.Fatal(err)
	}
	service, _ := newService(t, testTiming)
	serverCredentials := ca.Issue(t, transport.HistoryIngressURI)
	rpc := listener.Config{
		ListenAddress: "127.0.0.1:0",
		TLS:           &listener.TLS{CertFile: serverCredentials.CertFile, KeyFile: serverCredentials.KeyFile},
		Auth: []listener.Authentication{
			&listener.SPIFFEAuth{CAFile: serverCredentials.CAFile},
			&listener.KubernetesBearerAuth{Audiences: []string{testAudience}},
		},
	}
	resolver, err = resolver.WithListenerAuthentication(rpc, svids)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS, err := rpc.ServerCredentials(transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{session_ingresspb.SessionJournalIngress_Push_FullMethodName: auth.SVIDOrAccounts(resolver, nil)}
	var pushHandlers atomic.Int32
	server := grpc.NewServer(
		grpc.ChainStreamInterceptor(methods.StreamInterceptor(time.Second), func(server any, stream grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
			if info.FullMethod == session_ingresspb.SessionJournalIngress_Push_FullMethodName {
				pushHandlers.Add(1)
			}
			return handler(server, stream)
		}),
		grpc.Creds(serverTLS),
	)
	session_ingresspb.RegisterSessionJournalIngressServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connect := func(workload workloadauthtesting.Workload) testIngress {
		config := ca.Issue(t, workload.URI)
		security, err := config.ClientCredentials(transport.HistoryIngressURI)
		if err != nil {
			t.Fatal(err)
		}
		connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(security), grpc.WithNoProxy())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = connection.Close() })
		return testIngress{history: service.History, client: session_ingresspb.NewSessionJournalIngressClient(connection)}
	}
	session := uuid.New()
	// Mixed certificate and bearer credentials are rejected before Push reaches its handler.
	firstIngress := connect(first)
	mixed := firstIngress.open(t, "forged")
	mixed.send(announce(uuid.New(), 0, ""))
	if code := mixed.refused(); code != codes.Unauthenticated {
		t.Fatalf("mixed SVID and bearer credentials: %v", code)
	}
	if got := pushHandlers.Load(); got != 0 {
		t.Fatalf("mixed credentials reached Push handler %d times", got)
	}
	// The established SVID call retains its admitted writer through Sandbox deletion and final seal.
	admitted := firstIngress.open(t, "")
	admitted.send(append([]*session_ingresspb.PushFrame{announce(session, 0, "")}, entries(1)...)...)
	admitted.acknowledged(1, false)
	if got := pushHandlers.Load(); got != 1 {
		t.Fatalf("SVID call reached Push handler %d times, want 1", got)
	}
	refused := connect(other).open(t, "")
	refused.send(announce(session, 0, ""))
	if code := refused.refused(); code != codes.PermissionDenied {
		t.Fatalf("different SVID writer: %v", code)
	}
	if got := committed(t, service.History, session); len(got) != 1 {
		t.Fatalf("committed %v", got)
	}
	// Deletion retains the credential while Runtime flushes its final History seal.
	first.Sandbox.Finalizers = []string{"test.example/keep-until-sealed"}
	if err := first.Objects.Update(t.Context(), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	if err := first.Objects.Delete(t.Context(), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	if err := first.Objects.Get(t.Context(), client.ObjectKeyFromObject(first.Sandbox), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	first.Sandbox.Status.StopStage = sandboxapi.StopStageQuiescing
	if err := first.Objects.Status().Update(t.Context(), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	admitted.send(sealed(1, first.Principal.PodUID))
	admitted.acknowledged(1, true)

	// Clearing acceptance revokes subsequent calls on the existing TLS connection.
	first.Sandbox.Status.RunnerCredential.AcceptedID = ""
	if err := first.Objects.Status().Update(t.Context(), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	revoked := firstIngress.open(t, "")
	revoked.send(announce(uuid.New(), 0, ""))
	if code := revoked.refused(); code != codes.Unauthenticated {
		t.Fatalf("revoked credential: %v", code)
	}
}
