package main

import (
	"context"
	"crypto/x509"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

func TestSharedListenerPreservesHistoryReadAndWriterPolicies(t *testing.T) {
	workload := workloadauthtesting.NewWorkload(t, kube, "test-sandbox-namespace", "svid-"+uuid.NewString())
	ca := environmentruntimetransporttesting.NewCA(t)
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
	sampler := auth.Account{Namespace: "test-sandbox-namespace", Name: "test-sampler"}
	samplerToken, err := kube.WorkloadToken(t.Context(), sampler, "test-runner-ingress")
	if err != nil {
		t.Fatal(err)
	}
	service := start(t, func(c *config) {
		c.Kubeconfig = kubeconfig
		c.Ingress.SamplerAccounts = []auth.Account{sampler}
		c.RPC.TLS = &listener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile}
		c.RPC.Auth = append(c.RPC.Auth, &listener.SPIFFEAuth{CAFile: serving.CAFile})
	})
	// Backend bearer reads use the same TLS listener without presenting a certificate.
	_, err = service.reads.GetSessionSummary(bearing(t, readerToken), &historypb.GetSessionSummaryRequest{SessionId: uuid.NewString()})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("backend reader: %v", err)
	}
	leaf := ca.Issue(t, workload.URI)
	security, err := leaf.ClientCredentials(environment_runtime_transport.HistoryIngressURI)
	if err != nil {
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(service.rpcAddress, grpc.WithTransportCredentials(security))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	_, err = historypb.NewHistoryServiceClient(conn).GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: uuid.NewString()})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("SVID gained History reads: %v", err)
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
