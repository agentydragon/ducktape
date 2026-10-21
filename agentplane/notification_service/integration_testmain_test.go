package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/bazelbuild/rules_go/go/runfiles"
	_ "github.com/lib/pq"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

var (
	integrationDatabase *postgres.Database
	// apiConfig reaches the API server the service watches its send grants on, in testNamespace; apiClient
	// writes them.
	apiConfig *rest.Config
	apiClient client.Client
)

func TestMain(m *testing.M) {
	os.Exit(runIntegrationTests(m))
}

func runIntegrationTests(m *testing.M) int {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	database, err := postgres.Start(ctx, migrations.Up)
	cancel()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	integrationDatabase = database

	code := runWithAPIServer(m)
	if err := database.Close(); err != nil {
		fmt.Fprintln(os.Stderr, "close PostgreSQL fixture:", err)
		if code == 0 {
			code = 1
		}
	}
	return code
}

func runWithAPIServer(m *testing.M) int {
	asset, err := runfiles.Rlocation("envtest_kubernetes_linux_amd64/kube-apiserver")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	crd, err := runfiles.Rlocation("_main/agentplane/crds/manifests/crd-policybindings.yaml")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	existing := false
	environment := &envtest.Environment{
		BinaryAssetsDirectory: filepath.Dir(asset),
		CRDDirectoryPaths:     []string{filepath.Dir(crd)},
		ErrorIfCRDPathMissing: true,
		UseExistingCluster:    &existing,
	}
	if apiConfig, err = environment.Start(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	defer func() {
		if err := environment.Stop(); err != nil {
			fmt.Fprintln(os.Stderr, "stop API server:", err)
		}
	}()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
	}
	if apiClient, err = client.New(apiConfig, client.Options{Scheme: scheme}); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	if err := apiClient.Create(context.Background(), &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: testNamespace}}); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return m.Run()
}
