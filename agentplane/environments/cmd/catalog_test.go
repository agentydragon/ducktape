package main

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environments/internal/resume"
	environmentservice "github.com/agentydragon/ducktape/agentplane/environments/internal/service"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/testscheme"
	"google.golang.org/grpc"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestConfiguredHarnessNamesRoundTripThroughEnvironmentService(t *testing.T) {
	t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings+`environment_service:
  policy_namespace: agentplane
  rpc:
    listen_address: ":8082"
    tls: {cert_file: /tmp/server-cert, key_file: /tmp/server-key}
    auth: [{mode: spiffe, ca_file: /tmp/ca}]
  service_readers: [spiffe://agentplane/app]
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
	objects := clientfake.NewClientBuilder().WithScheme(testscheme.New(t)).WithObjects(sandbox, executionAccount).Build()
	identities, err := auth.KubernetesIdentityUIDs(objects, "agentplane")
	if err != nil {
		t.Fatal(err)
	}
	lifecycleManager, err := resume.NewManager(lifecycle.Options{
		Namespace: "agentplane", Objects: objects, Identities: identities,
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := environmentservice.New(
		"agentplane", objects, identities, config.RuntimeCatalog, lifecycleManager,
		environmentservice.Options{ServiceReaders: sets.New(config.ServiceReaders...)},
	)
	if err != nil {
		t.Fatal(err)
	}
	methods, err := environmentservice.WithServiceCallers(environmentservice.Methods(nil), config.ServiceReaders, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLS(t, peertls.SandboxControllerURI, methods, func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	}, peertls.AppURI)
	client := environmentpb.NewEnvironmentServiceClient(connection)
	ctx := grpcmethods.Caller(t.Context(), peertls.AppURI)
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
