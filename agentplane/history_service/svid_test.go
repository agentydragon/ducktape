package main

import (
	"context"
	"crypto/x509"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

// startWithSVIDs serves History with workload certificates from ca enabled on its listener.
func startWithSVIDs(t *testing.T, ca *environmentruntimetransporttesting.CA, tune func(*config)) running {
	t.Helper()
	serving := ca.IssueWithUsage(t, environment_runtime_transport.HistoryIngressURI, x509.ExtKeyUsageServerAuth)
	kubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	err := clientcmd.WriteToFile(clientcmdapi.Config{
		Clusters:  map[string]*clientcmdapi.Cluster{"test": {Server: kube.Config.Host, CertificateAuthorityData: kube.Config.CAData}},
		AuthInfos: map[string]*clientcmdapi.AuthInfo{"test": {ClientCertificateData: kube.Config.CertData, ClientKeyData: kube.Config.KeyData}},
		Contexts:  map[string]*clientcmdapi.Context{"test": {Cluster: "test", AuthInfo: "test"}}, CurrentContext: "test",
	}, kubeconfig)
	if err != nil {
		t.Fatal(err)
	}
	return start(t, func(c *config) {
		c.Kubeconfig = kubeconfig
		c.RPC.TLS = &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile}
		c.RPC.Auth = append(c.RPC.Auth, &listener.SPIFFEAuth{CAFile: serving.CAFile})
		if tune != nil {
			tune(c)
		}
	})
}

// svidConnection dials service presenting a certificate for a workload URI.
func svidConnection(t *testing.T, ca *environmentruntimetransporttesting.CA, service running, uri string) *grpc.ClientConn {
	t.Helper()
	security, err := ca.Issue(t, uri).ClientCredentials(environment_runtime_transport.HistoryIngressURI)
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
	ca := environmentruntimetransporttesting.NewCA(t)
	sampler := auth.Account{Namespace: "test-sandbox-namespace", Name: "test-sampler"}
	samplerToken, err := kube.WorkloadToken(t.Context(), sampler, "test-runner-ingress")
	if err != nil {
		t.Fatal(err)
	}
	service := startWithSVIDs(t, ca, func(c *config) {
		c.Ingress.SamplerAccounts = []auth.Account{sampler}
		c.PolicyNamespace = testPolicyNamespace
	})
	// Backend bearer reads use the same TLS listener without presenting a certificate.
	_, err = service.reads.GetSessionSummary(bearing(t, readerToken), &historypb.GetSessionSummaryRequest{SessionId: uuid.NewString()})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("backend reader: %v", err)
	}
	conn := svidConnection(t, ca, service, workload.URI)
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
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
		"mixed bearer": metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+samplerToken),
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
	// An ordinary writer bearer cannot bypass the sampler allowlist on the mixed listener.
	denied, err := service.ingress.Push(bearing(t, writerToken))
	if err != nil {
		t.Fatal(err)
	}
	_, err = denied.Recv()
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("non-sampler bearer: %v", err)
	}
	admitted, err := service.ingress.Push(bearing(t, samplerToken))
	if err != nil {
		t.Fatal(err)
	}
	// A valid sampler reaches handler validation; a missing announcement is a protocol error.
	if err := admitted.Send(&session_ingresspb.PushFrame{Frame: &session_ingresspb.PushFrame_Entry{Entry: journalEntry(1)}}); err != nil {
		t.Fatal(err)
	}
	_, err = admitted.Recv()
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("sampler admission: %v", err)
	}
}

const testPolicyNamespace = "test-policies"

// An Identity SVID reads exactly the Sessions retained as executing as its Identity incarnation: not
// another Identity's, and not its predecessor's once a same-named successor replaces it.
func TestAnIdentitySVIDReadsTheSessionsItsIdentityExecuted(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	first := workloadauthtesting.NewIdentityWorkload(t, kube, "test-sandbox-namespace", "test-first-"+uuid.NewString()[:8], testPolicyNamespace)
	other := workloadauthtesting.NewIdentityWorkload(t, kube, "test-sandbox-namespace", "test-other-"+uuid.NewString()[:8], testPolicyNamespace)
	service := startWithSVIDs(t, ca, func(c *config) { c.PolicyNamespace = testPolicyNamespace })
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
			t.Fatalf("the Identity runner's push ended unacknowledged: %v", err)
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
