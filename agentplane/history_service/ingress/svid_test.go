package ingress

import (
	"net"
	"testing"
	"time"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
)

func TestSVIDKeepsTheFirstWriterServiceAccountGate(t *testing.T) {
	namespace := "history-svid-" + uuid.NewString()
	first := workloadauthtesting.NewWorkload(t, kube, namespace, "first-writer")
	other := workloadauthtesting.NewWorkload(t, kube, namespace, "other-writer")
	ca := transporttesting.NewCA(t)
	resolver, err := auth.NewKubernetesSVIDResolver(kube.Config, []string{namespace})
	if err != nil {
		t.Fatal(err)
	}
	service, _ := newService(t, testTiming)
	service.Principals = service.Principals.WithSVID(resolver)
	serverCredentials := ca.Issue(t, transport.HistoryIngressURI)
	serverTLS, err := serverCredentials.WorkloadServerTLSConfig(transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{session_ingresspb.SessionJournalIngress_Push_FullMethodName: auth.AnyAccount(service.Principals)}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
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
	// A forged bearer on the real TLS call cannot replace the certificate's writer.
	firstIngress := connect(first)
	admitted := firstIngress.open(t, "forged")
	admitted.send(append([]*session_ingresspb.PushFrame{announce(session, 0, "")}, entries(1)...)...)
	admitted.acknowledged(1, false)
	refused := connect(other).open(t, writerToken)
	refused.send(announce(session, 0, ""))
	if code := refused.refused(); code != codes.PermissionDenied {
		t.Fatalf("different SVID writer: %v", code)
	}
	if got := committed(t, service.History, session); len(got) != 1 {
		t.Fatalf("committed %v", got)
	}
	// Live Sandbox revocation also applies to a later call on an existing TLS connection.
	if err := first.Objects.Delete(t.Context(), first.Sandbox); err != nil {
		t.Fatal(err)
	}
	revoked := firstIngress.open(t, writerToken)
	revoked.send(announce(uuid.New(), 0, ""))
	if code := revoked.refused(); code != codes.Unauthenticated {
		t.Fatalf("deleted Sandbox: %v", code)
	}
}
