package main

import (
	"fmt"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/util/testing/nettest"
	"gopkg.in/yaml.v3"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// namespaces numbers the namespaces tests work in.
var namespaces atomic.Int32

func newCatalog(t *testing.T, document string) *catalog.Catalog {
	t.Helper()
	var settings catalog.Settings
	if err := yaml.Unmarshal([]byte(document), &settings); err != nil {
		t.Fatal(err)
	}
	groups, err := catalog.New(settings)
	if err != nil {
		t.Fatal(err)
	}
	return groups
}

// sandboxNamespace is a namespace of kube's holding the test template, whose Pod has the egress sidecar, or not.
func sandboxNamespace(t *testing.T, kube *kubeapiserver.Server, sidecar bool) string {
	t.Helper()
	namespace := fmt.Sprintf("sandbox-test-%d", namespaces.Add(1))
	if _, err := kube.ServiceAccount(t.Context(), auth.Account{Namespace: namespace, Name: "test-caller"}); err != nil {
		t.Fatal(err)
	}
	containers := []corev1.Container{{Name: "workspace", Image: "registry.test/unused"}}
	if sidecar {
		containers = append(containers, corev1.Container{Name: "egress-sidecar", Image: "registry.test/unused"})
	}
	_, err := kube.Clientset.CoreV1().PodTemplates(namespace).Create(t.Context(), &corev1.PodTemplate{
		ObjectMeta: metav1.ObjectMeta{Name: "test-template"},
		Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{
			AutomountServiceAccountToken: new(false), Containers: containers,
		}},
	}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return namespace
}

// sandboxGroups is a sandbox group over namespace offering directTools, beside an MCP group whose server refuses
// every connection.
func sandboxGroups(t *testing.T, namespace, directTools string) *catalog.Catalog {
	t.Helper()
	refusing := nettest.RefusingAddress(t)
	return newCatalog(t, `{
		"test-sandbox": {"title": "t", "description": "d", "direct_tools": `+directTools+`,
			"executor": {"kind": "pod", "description": "e", "namespace": "`+namespace+`",
				"templates": {"test-template": "the test box"}}},
		"test-mcp": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e",
			"config": {"transport": "streamable-http", "url": "http://`+refusing+`/mcp", "auth": "none"}}}
	}`)
}
