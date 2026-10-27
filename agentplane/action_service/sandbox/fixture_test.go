package sandbox

import (
	"context"
	"fmt"
	"io"
	"os"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const testTemplate = "test-template"

var (
	server     *kubeapiserver.Server
	namespaces atomic.Int32
)

// TestMain runs one API server for every test; each test works in a namespace of its own. No kubelet runs, so a box
// never starts: a test writes the status Kubernetes would.
func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(started *kubeapiserver.Server) error {
		server = started
		return nil
	}))
}

// fixture is a namespace holding the test template, and a caller of its own.
type fixture struct {
	binding catalog.PodBinding
	caller  action.ServiceAccount
	exec    *recordingExec
}

// newFixture makes the namespace, the caller's ServiceAccount and the test template. edit changes the template's
// Pod before it is stored.
func newFixture(t *testing.T, edit func(*corev1.PodSpec)) fixture {
	t.Helper()
	namespace := fmt.Sprintf("sandbox-test-%d", namespaces.Add(1))
	f := fixture{
		binding: catalog.PodBinding{
			Description: "test sandboxes", Namespace: namespace, Templates: map[string]string{testTemplate: "the test box"},
			MaxTimeoutSeconds: 60, MaxOutputBytes: 1000,
		},
		caller: account(t, namespace, "test-caller"),
		exec:   &recordingExec{run: localExec},
	}
	spec := corev1.PodSpec{
		AutomountServiceAccountToken: new(false),
		Containers: []corev1.Container{
			{Name: "workspace", Image: "registry.test/unused"},
			{Name: egressSidecar, Image: "registry.test/unused"},
		},
	}
	if edit != nil {
		edit(&spec)
	}
	_, err := server.Clientset.CoreV1().PodTemplates(namespace).Create(t.Context(), &corev1.PodTemplate{
		ObjectMeta: metav1.ObjectMeta{Name: testTemplate},
		Template: corev1.PodTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app.kubernetes.io/name": "test-box"}},
			Spec:       spec,
		},
	}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return f
}

// account is a ServiceAccount of that name, created with its namespace.
func account(t *testing.T, namespace, name string) action.ServiceAccount {
	t.Helper()
	if _, err := server.ServiceAccount(t.Context(), auth.Account{Namespace: namespace, Name: name}); err != nil {
		t.Fatal(err)
	}
	return action.ServiceAccount{Namespace: namespace, Name: name}
}

func (f fixture) inventory() *Inventory {
	return NewInventory(f.binding, server.Clientset, f.exec.Exec)
}

func (f fixture) pod(t *testing.T, caller action.ServiceAccount, name string) *corev1.Pod {
	t.Helper()
	pod, err := server.Clientset.CoreV1().Pods(f.binding.Namespace).Get(t.Context(), objectName(caller, name), metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return pod
}

// report writes the status Kubernetes would to the caller's box of that name.
func (f fixture) report(t *testing.T, caller action.ServiceAccount, name string, status corev1.PodStatus) {
	t.Helper()
	pod := f.pod(t, caller, name)
	pod.Status = status
	if _, err := server.Clientset.CoreV1().Pods(f.binding.Namespace).UpdateStatus(t.Context(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
}

// ready is the status of a box that can run something.
var ready = corev1.PodStatus{
	Phase:      corev1.PodRunning,
	Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}},
}

// target is where a command was asked to run.
type target struct {
	pod       types.NamespacedName
	container string
}

// recordingExec is an Exec that records where each command was asked to run before running it.
type recordingExec struct {
	run Exec
	ran []target
}

func (r *recordingExec) Exec(ctx context.Context, pod types.NamespacedName, container string, command []string, stdout, stderr io.Writer) error {
	r.ran = append(r.ran, target{pod, container})
	return r.run(ctx, pod, container, command, stdout, stderr)
}
