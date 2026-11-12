package sandbox

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"os"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	workloadidentitytesting "github.com/agentydragon/ducktape/agentplane/identity/credentials/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const testTemplate = "test-template"

var (
	server     *kubeapiserver.Server
	objects    client.Client
	namespaces atomic.Int32
)

// TestMain runs one API server for every test; each test works in a namespace of its own. No kubelet runs, so a box
// never starts: a test writes the status Kubernetes would. Nor do the Policy Controller and cert-manager: a test
// plays them for its namespace's credentials.
func TestMain(m *testing.M) {
	certificates, err := workloadidentitytesting.CertificateCRD()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(kubeapiserver.Run(m, func(started *kubeapiserver.Server) error {
		server = started
		scheme := runtime.NewScheme()
		for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, workloadapi.AddToScheme, policyapi.AddToScheme} {
			if err := add(scheme); err != nil {
				return err
			}
		}
		objects, err = client.New(started.Config, client.Options{Scheme: scheme})
		return err
	}, certificates))
}

// fixture is a namespace holding the test template and its delegated workload Identity.
type fixture struct {
	binding           catalog.PodBinding
	identityNamespace string
	caller            Identity
	exec              *recordingExec
	issuance          workloadidentitytesting.Issuance
}

// newFixture makes the box namespace, policy namespace, workload Identity, and test template. edit changes the template's
// Pod before it is stored.
func newFixture(t *testing.T, edit func(*corev1.PodSpec)) fixture {
	t.Helper()
	namespace := fmt.Sprintf("sandbox-test-%d", namespaces.Add(1))
	identityNamespace := namespace + "-policy"
	for _, name := range []string{namespace, identityNamespace} {
		if err := objects.Create(t.Context(), &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}); err != nil {
			t.Fatal(err)
		}
	}
	f := fixture{
		binding: catalog.PodBinding{
			Description: "test sandboxes", Namespace: namespace, Templates: map[string]string{testTemplate: "the test box"},
			MaxTimeoutSeconds: 60, MaxOutputBytes: 1000,
		},
		identityNamespace: identityNamespace,
		caller:            workloadIdentity(t, identityNamespace, "test-agent"),
		exec:              &recordingExec{run: localExec},
		issuance:          workloadidentitytesting.Issuance{Objects: objects, CA: transporttesting.NewCA(t), Namespace: namespace},
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

// workloadIdentity creates an Identity in a separate policy namespace and returns its pinned incarnation.
func workloadIdentity(t *testing.T, namespace, name string) Identity {
	t.Helper()
	live := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name},
		Spec:       policyapi.IdentitySpec{Description: "test agent"},
	}
	if err := objects.Create(t.Context(), live); err != nil {
		t.Fatal(err)
	}
	return Identity{Name: live.Name, UID: string(live.UID)}
}

func (f fixture) inventory() *Inventory {
	credentials, err := requester.New(objects, objects)
	if err != nil {
		panic(err) // Both are given.
	}
	return NewInventory(f.binding, server.Clientset, f.exec.Exec, credentials, objects)
}

// created is what Create returned, with the box's credential issued as it waited.
type created struct {
	info Info
	err  error
}

// create is Create, with the box's credential issued as it waits.
func (f fixture) create(t *testing.T, inventory *Inventory, caller Identity, name, template string) (Info, error) {
	t.Helper()
	result := workloadidentitytesting.While(t, f.issuance, func() created {
		info, err := inventory.Create(t.Context(), caller, name, template)
		return created{info, err}
	})
	return result.info, result.err
}

// leaf is the certificate the record's Secret holds.
func (f fixture) leaf(t *testing.T, record *workloadapi.WorkloadCredential) *x509.Certificate {
	t.Helper()
	secret, err := server.Clientset.CoreV1().Secrets(f.binding.Namespace).Get(t.Context(), record.Status.SecretName, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	pair, err := tls.X509KeyPair(secret.Data[corev1.TLSCertKey], secret.Data[corev1.TLSPrivateKeyKey])
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	return leaf
}

// one is the one item of items that matches.
func one[T any](t *testing.T, items []T, matches func(T) bool) T {
	t.Helper()
	var found []T
	for _, item := range items {
		if matches(item) {
			found = append(found, item)
		}
	}
	if len(found) != 1 {
		t.Fatalf("%d items match in %+v, want one", len(found), items)
	}
	return found[0]
}

// record is the box's WorkloadCredential.
func (f fixture) record(t *testing.T, caller Identity, name string) *workloadapi.WorkloadCredential {
	t.Helper()
	var record workloadapi.WorkloadCredential
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: f.binding.Namespace, Name: objectName(caller, name)}, &record); err != nil {
		t.Fatal(err)
	}
	return &record
}

func (f fixture) pod(t *testing.T, caller Identity, name string) *corev1.Pod {
	t.Helper()
	pod, err := server.Clientset.CoreV1().Pods(f.binding.Namespace).Get(t.Context(), objectName(caller, name), metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return pod
}

// report writes the status Kubernetes would to the caller's box of that name.
func (f fixture) report(t *testing.T, caller Identity, name string, status corev1.PodStatus) {
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
