package main

import (
	"context"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	_ "github.com/lib/pq"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"

	"sigs.k8s.io/controller-runtime/pkg/client"
)

var (
	integrationDatabase *postgres.Database
	kube                *kubeapiserver.Server
	// apiConfig reaches the API server the service watches its send grants on, in testNamespace; apiClient
	// writes them.
	apiConfig *rest.Config
	apiClient client.Client
	// sandboxToken proves the ServiceAccount of the Sandbox sandboxName, which a Sandbox runs as by its own
	// name; otherToken proves otherName's, whose Sandbox does not exist.
	sandboxToken, otherToken string
)

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		migrations.Up,
		func(database *postgres.Database) int {
			integrationDatabase = database
			return kubeapiserver.Run(m, useAPIServer)
		},
	))
}

func useAPIServer(server *kubeapiserver.Server) (err error) {
	kube, apiConfig = server, server.Config
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			return err
		}
	}
	if apiClient, err = client.New(apiConfig, client.Options{Scheme: scheme}); err != nil {
		return err
	}
	ctx := context.Background()
	if err := apiClient.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: testNamespace}}); err != nil {
		return err
	}
	if sandboxToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: testNamespace, Name: sandboxName}, testAudience); err != nil {
		return err
	}
	otherToken, err = server.WorkloadToken(ctx, auth.Account{Namespace: testNamespace, Name: otherName}, testAudience)
	return err
}
