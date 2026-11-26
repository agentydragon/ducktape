package main

import (
	"context"
	"crypto/x509"
	"net"
	"path/filepath"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

// svidConfig configures History with a verified workload listener and the envtest API.
func svidConfig(t *testing.T, ca *peertlstesting.CA) config {
	t.Helper()
	serving := ca.IssueWithUsage(t, peertls.HistoryIngressURI, x509.ExtKeyUsageServerAuth)
	kubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	err := clientcmd.WriteToFile(clientcmdapi.Config{
		Clusters:  map[string]*clientcmdapi.Cluster{"test": {Server: kube.Config.Host, CertificateAuthorityData: kube.Config.CAData}},
		AuthInfos: map[string]*clientcmdapi.AuthInfo{"test": {ClientCertificateData: kube.Config.CertData, ClientKeyData: kube.Config.KeyData}},
		Contexts:  map[string]*clientcmdapi.Context{"test": {Cluster: "test", AuthInfo: "test"}}, CurrentContext: "test",
	}, kubeconfig)
	if err != nil {
		t.Fatal(err)
	}
	c := defaultConfig()
	c.DatabaseURL = integrationDatabase.URL
	c.Kubeconfig = kubeconfig
	c.RPC.TLS = &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile}
	c.RPC.Auth = []*listener.SPIFFEAuth{
		{CAFile: serving.CAFile},
	}
	c.ReaderServiceCallers = []string{peertls.AppURI}
	c.Ingress.WriterNamespaces = []string{"test-sandbox-namespace"}
	c.FoldIntervalSeconds = 3600
	return c
}

// startWithSVIDs serves History with workload certificates from ca enabled on its listener.
func startWithSVIDs(t *testing.T, ca *peertlstesting.CA, tune func(*config)) running {
	t.Helper()
	c := svidConfig(t, ca)
	if tune != nil {
		tune(&c)
	}
	workload := workloadauthtesting.NewIdentityWorkload(
		t, kube, "test-sandbox-namespace", "history-runner-"+uuid.NewString()[:8], testPolicyNamespace,
	)
	rpcListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	served := make(chan error, 1)
	go func() { served <- serve(ctx, c, rpcListener, testIdentities(t)) }()
	var stopped bool
	stop := func() error {
		if stopped {
			return nil
		}
		stopped = true
		cancel()
		select {
		case err := <-served:
			return err
		case <-time.After(callDeadline):
			return context.DeadlineExceeded
		}
	}
	t.Cleanup(func() { _ = stop() })
	readerSecurity, err := ca.Issue(t, peertls.AppURI).ClientCredentials(peertls.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	readerConnection, err := grpc.NewClient(rpcListener.Addr().String(), grpc.WithTransportCredentials(readerSecurity))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = readerConnection.Close() })
	runnerConnection, err := ca.Issue(t, workload.URI).ClientCredentials(peertls.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	ingressConnection, err := grpc.NewClient(rpcListener.Addr().String(), grpc.WithTransportCredentials(runnerConnection))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ingressConnection.Close() })
	return running{
		rpcAddress: rpcListener.Addr().String(),
		reads:      historypb.NewHistoryServiceClient(readerConnection),
		health:     healthpb.NewHealthClient(readerConnection),
		ingress:    session_ingresspb.NewSessionJournalIngressClient(ingressConnection),
		runner:     &identitypb.Identity{Name: workload.Identity.Name, Uid: string(workload.Identity.UID)},
		stop:       stop,
	}
}

// svidConnection dials service presenting a certificate for a workload URI.
func svidConnection(t *testing.T, ca *peertlstesting.CA, service running, uri string) *grpc.ClientConn {
	t.Helper()
	security, err := ca.Issue(t, uri).ClientCredentials(peertls.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(service.rpcAddress, grpc.WithTransportCredentials(security))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return conn
}

func TestSharedListenerPreservesHistoryReadAndWriterPolicies(t *testing.T) {
	workload := workloadauthtesting.NewIdentityWorkload(t, kube, "test-sandbox-namespace", "svid-"+uuid.NewString(), testPolicyNamespace).Workload
	ca := peertlstesting.NewCA(t)
	service := startWithSVIDs(t, ca, nil)
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	_, err := service.reads.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: uuid.NewString()})
	// The explicitly granted App SVID reads through the shared production listener.
	if status.Code(err) != codes.NotFound {
		t.Fatalf("backend reader: %v", err)
	}
	conn := svidConnection(t, ca, service, workload.URI)
	_, err = historypb.NewHistoryServiceClient(conn).GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: uuid.NewString()})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("an Identity SVID read outside its retained Sessions: %v", err)
	}
	journal := session_ingresspb.NewSessionJournalIngressClient(conn)
	push, err := journal.Push(ctx)
	if err != nil {
		t.Fatal(err)
	}
	sessionID := uuid.NewString()
	if err := push.Send(&session_ingresspb.PushFrame{Frame: &session_ingresspb.PushFrame_Announce{Announce: &sessionpb.Attached{SessionId: sessionID}}}); err != nil {
		t.Fatal(err)
	}
	if err := push.Send(&session_ingresspb.PushFrame{Frame: &session_ingresspb.PushFrame_Entry{Entry: journalEntry(1)}}); err != nil {
		t.Fatal(err)
	}
	for {
		ack, err := push.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if ack.GetCursor() == 1 {
			break
		}
	}
	for name, attempt := range map[string]context.Context{
		"mixed bearer": metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken),
		"delegation":   metadata.AppendToOutgoingContext(ctx, auth.DelegationMetadataKey, "forged"),
	} {
		stream, err := journal.Push(attempt)
		if err != nil {
			t.Fatal(err)
		}
		_, err = stream.Recv()
		if status.Code(err) != codes.Unauthenticated {
			t.Fatalf("%s: %v", name, err)
		}
	}
	noClientCertificate, err := (&peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: peertls.HistoryIngressURI}).Credentials()
	if err != nil {
		t.Fatal(err)
	}
	bareConnection, err := grpc.NewClient(service.rpcAddress, grpc.WithTransportCredentials(noClientCertificate))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = bareConnection.Close() })
	barePush, err := session_ingresspb.NewSessionJournalIngressClient(bareConnection).Push(
		metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+writerToken),
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := barePush.Recv(); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("bearer-only Push: %v", err)
	}
}

const testPolicyNamespace = "test-policies"

// testIdentities reads the test API server's Identities in testPolicyNamespace, standing in for the PolicyService view.
func testIdentities(t *testing.T) auth.IdentityUIDReader {
	t.Helper()
	identities, err := auth.NewKubernetesIdentityUIDs(kube.Config, testPolicyNamespace)
	if err != nil {
		t.Fatal(err)
	}
	return identities
}

// An Identity SVID reads exactly the Sessions retained as executing as its Identity incarnation: not
// another Identity's, and not its predecessor's once a same-named successor replaces it.
func TestAnIdentitySVIDReadsTheSessionsItsIdentityExecuted(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	first := workloadauthtesting.NewIdentityWorkload(t, kube, "test-sandbox-namespace", "test-first-"+uuid.NewString()[:8], testPolicyNamespace)
	other := workloadauthtesting.NewIdentityWorkload(t, kube, "test-sandbox-namespace", "test-other-"+uuid.NewString()[:8], testPolicyNamespace)
	service := startWithSVIDs(t, ca, nil)
	firstConnection := svidConnection(t, ca, service, first.URI)
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()

	sessionID := uuid.New()
	push, err := session_ingresspb.NewSessionJournalIngressClient(firstConnection).Push(ctx)
	if err != nil {
		t.Fatal(err)
	}
	announce := &sessionpb.Attached{
		SessionId:         sessionID.String(),
		ExecutionIdentity: &identitypb.Identity{Name: first.Identity.Name, Uid: string(first.Identity.UID)},
	}
	for _, frame := range []*session_ingresspb.PushFrame{
		{Frame: &session_ingresspb.PushFrame_Announce{Announce: announce}},
		{Frame: &session_ingresspb.PushFrame_Entry{Entry: journalEntry(1)}},
	} {
		if err := push.Send(frame); err != nil {
			t.Fatal(err)
		}
	}
	for acknowledged := uint64(0); acknowledged < 1; {
		ack, err := push.Recv()
		if err != nil {
			t.Fatalf("the Environment Runtime's push ended unacknowledged: %v", err)
		}
		acknowledged = ack.GetCursor()
	}

	read := func(connection *grpc.ClientConn) error {
		_, err := historypb.NewHistoryServiceClient(connection).ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{SessionId: sessionID.String(), Limit: 10})
		return err
	}
	if err := read(firstConnection); err != nil {
		t.Fatalf("an Identity could not read the Session it executed: %v", err)
	}
	if err := read(svidConnection(t, ca, service, other.URI)); status.Code(err) != codes.NotFound {
		t.Fatalf("another Identity read the Session: %v", err)
	}

	if err := first.Objects.Delete(ctx, first.Identity); err != nil {
		t.Fatal(err)
	}
	successor := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: testPolicyNamespace, Name: first.Identity.Name},
		Spec:       policyapi.IdentitySpec{Description: "test successor agent"},
	}
	if err := first.Objects.Create(ctx, successor); err != nil {
		t.Fatal(err)
	}
	successorWorkload := workloadauthtesting.RunAs(t, workloadauthtesting.NewWorkload(t, kube, "test-sandbox-namespace", "test-successor-"+uuid.NewString()[:8]), successor)
	if err := read(svidConnection(t, ca, service, successorWorkload.URI)); status.Code(err) != codes.NotFound {
		t.Fatalf("a same-named successor Identity read its predecessor's Session: %v", err)
	}
}

// Exercise the production listener wiring, not a test-owned TLS server. Service certificates
// prove the peer while the read allowlist remains the authority for each method.
func TestSharedListenerAdmitsOnlyGrantedServiceReaders(t *testing.T) {
	ca := peertlstesting.NewCA(t)
	service := startWithSVIDs(t, ca, func(c *config) {
		c.ReaderServiceCallers = []string{peertls.AppURI}
	})
	for _, attempt := range []struct {
		name   string
		uri    string
		bearer bool
		want   codes.Code
	}{
		{"granted App", peertls.AppURI, false, codes.NotFound},
		{"ungranted service", peertls.NotificationsURI, false, codes.PermissionDenied},
		{"mixed bearer cannot rescue ungranted peer", peertls.NotificationsURI, true, codes.Unauthenticated},
	} {
		t.Run(attempt.name, func(t *testing.T) {
			leaf := ca.Issue(t, attempt.uri)
			security, err := (&peertls.ClientTLS{
				CAFile: leaf.CAFile, ServerURI: peertls.HistoryIngressURI,
				CertFile: leaf.CertFile, KeyFile: leaf.KeyFile, ClientURI: attempt.uri,
			}).Credentials()
			if err != nil {
				t.Fatal(err)
			}
			conn, err := grpc.NewClient(service.rpcAddress, grpc.WithTransportCredentials(security))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
			defer cancel()
			if attempt.bearer {
				ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken)
			}
			_, err = historypb.NewHistoryServiceClient(conn).GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: uuid.NewString()})
			if status.Code(err) != attempt.want {
				t.Fatalf("service reader: got %v, want %v", err, attempt.want)
			}
		})
	}
}
