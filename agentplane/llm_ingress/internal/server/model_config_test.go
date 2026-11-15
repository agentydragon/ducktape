package server

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestModelConfigServicePeerOverMTLS(t *testing.T) {
	workload := workloadauthtesting.NewIdentityWorkload(t, api, testNamespace, "llm-service-model-reader", testPolicyNamespace)
	svid, err := auth.NewSVIDResolver(workload.Objects, []string{testNamespace}, &resolver.Identities{PolicyNamespace: testPolicyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	rule, err := auth.AnySubject(svid).AllowServiceCallers([]string{peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	ca := transporttesting.NewCA(t)
	serverCredentials := ca.Issue(t, peertls.LLMIngressURI)
	server := newModelConfigServer(rule, &modelConfigService{
		models: map[string]modelConfig{"configured": {Model: "configured", TotalContextBudgetTokens: 8192}},
	})
	address := serveWorkloadModelConfigHTTP2(t, server, serverCredentials)
	clientLeaf := ca.Issue(t, peertls.AppURI)
	connection := newWorkloadModelConfigGRPCClient(t, address, clientLeaf, clientLeaf)
	client := modelconfigpb.NewModelConfigServiceClient(connection)
	request := &modelconfigpb.GetModelConfigRequest{Model: "configured"}
	response, err := client.GetModelConfig(t.Context(), request)
	if err != nil || response.TotalContextBudgetTokens == nil || response.GetTotalContextBudgetTokens() != 8192 {
		t.Fatalf("configured lookup: %v, %v", response, err)
	}
	response, err = client.GetModelConfig(t.Context(), &modelconfigpb.GetModelConfigRequest{Model: "unknown"})
	if err != nil || response.TotalContextBudgetTokens != nil {
		t.Fatalf("unknown lookup: %v, %v", response, err)
	}
	strangerLeaf := ca.Issue(t, peertls.NotificationsURI)
	stranger := newWorkloadModelConfigGRPCClient(t, address, strangerLeaf, strangerLeaf)
	if _, err := modelconfigpb.NewModelConfigServiceClient(stranger).GetModelConfig(t.Context(), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ungranted service lookup: %v", err)
	}
	ambiguous := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer test-token")
	if _, err := client.GetModelConfig(ambiguous, request); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("service certificate plus bearer lookup: %v", err)
	}
}
