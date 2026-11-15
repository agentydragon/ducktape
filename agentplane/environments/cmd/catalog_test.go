package main

import (
	"context"
	"testing"

	environmentservice "github.com/agentydragon/ducktape/agentplane/environments/internal/service"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type configTestTokenReviews struct{}

func (configTestTokenReviews) Create(
	_ context.Context,
	request *authenticationv1.TokenReview,
	_ metav1.CreateOptions,
) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     request.Spec.Audiences,
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:agentplane:app",
			UID:      "app-account-uid",
		},
	}}, nil
}

func TestConfiguredHarnessNamesRoundTripThroughEnvironmentService(t *testing.T) {
	t.Setenv(configFileEnv, writeSettings(t, validSettings+`environment_service:
  policy_namespace: agentplane
  audience: session-dispatcher
  rpc:
    listen_address: ":8082"
    tls: {cert_file: /tmp/server-cert, key_file: /tmp/server-key}
    auth: [{mode: spiffe, ca_file: /tmp/ca}]
  readers: [agentplane/app]
  runtime_catalog:
    models:
      models:
        - model: model-a
          display_name: Model A
          reasoning_efforts: [high]
      harnesses:
        HARNESS_CLAUDE: [model-a]
    templates:
      runner-vm:
        workspace_root: /state/workspaces
        workspace_provenance: DEPLOYMENT_CONFIG
        workspace_root_enforced: false
        creates_missing_cwd: true
`))
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	config := c.EnvironmentService
	if got := config.RuntimeCatalog.Models.Harnesses; len(got) != 1 || len(got["HARNESS_CLAUDE"]) != 1 || got["HARNESS_CLAUDE"][0] != "model-a" {
		t.Fatalf("loadConfig returned noncanonical harness catalog: %#v", got)
	}

	scheme := runtime.NewScheme()
	if err := api.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	sandboxUID := types.UID("sandbox-uid")
	controller := true
	sandbox := &api.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: "agentplane", Name: "workspace", UID: sandboxUID},
		Spec:       api.SandboxSpec{Template: "runner-vm"},
	}
	executionAccount := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: "agentplane", Name: "workspace", UID: "execution-account-uid",
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: api.GroupVersion.String(), Kind: "Sandbox", Name: "workspace", UID: sandboxUID, Controller: &controller,
		}},
	}}
	objects := clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(sandbox, executionAccount).Build()
	lifecycleManager, err := lifecycle.NewEnvironmentServiceManager(lifecycle.EnvironmentServiceOptions{
		Namespace: "agentplane", Objects: objects,
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := environmentservice.New(
		"agentplane", objects, config.RuntimeCatalog, lifecycleManager,
		sets.New(config.Readers...), environmentservice.ManagementOptions{},
	)
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(configTestTokenReviews{}, config.Audience, []string{"agentplane"}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.Serve(t, environmentservice.Methods(resolver), func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	})
	client := environmentpb.NewEnvironmentServiceClient(connection)
	ctx := metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer test-token")
	response, err := client.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil {
		t.Fatal(err)
	}
	if len(response.Items) != 1 || len(response.Items[0].GetConfiguration().GetConfigurations()) != 1 {
		t.Fatalf("discovery response = %+v", response)
	}
	harnessName := response.Items[0].GetConfiguration().GetConfigurations()[0].GetHarness()
	harnessValue, ok := sessionpb.Harness_value[harnessName]
	if !ok {
		t.Fatalf("discovery returned harness %q, which does not decode as SessionSpec.Harness", harnessName)
	}
	harness := sessionpb.Harness(harnessValue)
	if harness != sessionpb.Harness_HARNESS_CLAUDE {
		t.Fatalf("discovery harness decoded to %s", harness)
	}
}
