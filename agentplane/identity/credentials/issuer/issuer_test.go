package issuer

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"log/slog"
	"os"
	"testing"
	"time"

	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/bazelbuild/rules_go/go/runfiles"
	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
)

const (
	testNamespace  = "test-issuance"
	testIssuer     = "test-workload-issuer"
	subjectAccount = "test-agent"
)

var (
	objects client.Client
	sandbox = schema.GroupVersionKind{Group: "agentplane.allegedly.works", Version: "v1alpha1", Kind: "Sandbox"}
)

func TestMain(m *testing.M) {
	os.Exit(run(m))
}

// run serves the tests with the issuer running in a manager, and stops the manager before the
// control plane.
func run(m *testing.M) int {
	ctrl.SetLogger(logr.FromSlogHandler(slog.NewTextHandler(os.Stderr, nil)))
	certificateCRD, err := runfiles.Rlocation(os.Getenv("CERT_MANAGER_CERTIFICATE_CRD"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	server, err := kubeapiserver.Start(kubeapiserver.WithCRDs(certificateCRD))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	defer func() {
		if err := server.Stop(); err != nil {
			fmt.Fprintln(os.Stderr, err)
		}
	}()
	ctx, cancel := context.WithCancel(context.Background())
	stopped, err := startIssuer(ctx, server)
	if err != nil {
		cancel()
		fmt.Fprintln(os.Stderr, "start the issuer:", err)
		return 1
	}
	code := m.Run()
	cancel()
	if err := <-stopped; err != nil {
		fmt.Fprintln(os.Stderr, "manager:", err)
		code = 1
	}
	return code
}

func startIssuer(ctx context.Context, server *kubeapiserver.Server) (<-chan error, error) {
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, workloadapi.AddToScheme} {
		if err := add(scheme); err != nil {
			return nil, err
		}
	}
	var err error
	if objects, err = client.New(server.Config, client.Options{Scheme: scheme}); err != nil {
		return nil, err
	}
	if err := objects.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: testNamespace}}); err != nil {
		return nil, err
	}
	if err := objects.Create(ctx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: subjectAccount}}); err != nil {
		return nil, err
	}
	mgr, err := ctrl.NewManager(server.Config, ctrl.Options{
		Scheme:  scheme,
		Metrics: metricsserver.Options{BindAddress: "0"},
		Cache:   cache.Options{DefaultNamespaces: map[string]cache.Config{testNamespace: {}}},
	})
	if err != nil {
		return nil, err
	}
	if err := (&Reconciler{Client: mgr.GetClient(), APIReader: mgr.GetAPIReader(), Issuer: testIssuer}).SetupWithManager(mgr); err != nil {
		return nil, err
	}
	stopped := make(chan error, 1)
	go func() { stopped <- mgr.Start(ctx) }()
	return stopped, nil
}

func eventually(t *testing.T, what string, check func() (bool, string)) {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for {
		ok, state := check()
		if ok {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s; last: %s", what, state)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func owner(name string) requester.Owner {
	return requester.OwnedBy(&metav1.ObjectMeta{Namespace: testNamespace, Name: name, UID: types.UID("0f0e0d0c-0b0a-4908-8706-" + fmt.Sprintf("%012x", len(name)))}, sandbox)
}

func newRequester(t *testing.T) requester.Requester {
	t.Helper()
	requests, err := requester.New(objects, objects)
	if err != nil {
		t.Fatal(err)
	}
	return requests
}

func get(t *testing.T, name string) *workloadapi.WorkloadCredential {
	t.Helper()
	var record workloadapi.WorkloadCredential
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: name}, &record); err != nil {
		t.Fatal(err)
	}
	return &record
}

// observed waits for the issuer to compute status for the record's current generation.
func observed(t *testing.T, name string) *workloadapi.WorkloadCredential {
	t.Helper()
	var record *workloadapi.WorkloadCredential
	eventually(t, "the issuer to observe "+name, func() (bool, string) {
		record = get(t, name)
		return record.Status.ObservedGeneration == record.Generation && record.Status.CredentialID != "", fmt.Sprintf("%+v", record.Status)
	})
	return record
}

func certificate(t *testing.T, name string) (*unstructured.Unstructured, error) {
	t.Helper()
	certificate := NewCertificate()
	return certificate, objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: name}, certificate)
}

// issue does what cert-manager would for the Certificate named in the record's status: marks it
// Ready and writes its Secret, labelled from secretTemplate.
func issue(t *testing.T, ca *transporttesting.CA, record *workloadapi.WorkloadCredential) {
	t.Helper()
	ctx := t.Context()
	var issued *unstructured.Unstructured
	eventually(t, "Certificate "+record.Status.SecretName, func() (bool, string) {
		var err error
		issued, err = certificate(t, record.Status.SecretName)
		return err == nil, fmt.Sprint(err)
	})
	labels, _, _ := unstructured.NestedStringMap(issued.Object, "spec", "secretTemplate", "labels")
	uris, _, _ := unstructured.NestedStringSlice(issued.Object, "spec", "uris")
	config := ca.IssueWithUsage(t, uris[0], x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth)
	pair := map[string][]byte{}
	for key, file := range map[string]string{corev1.TLSCertKey: config.CertFile, corev1.TLSPrivateKeyKey: config.KeyFile} {
		content, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		pair[key] = content
	}
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: testNamespace, Name: record.Status.SecretName, Labels: labels,
			Annotations: map[string]string{certificateNameAnnotation: issued.GetName()},
		},
		Type: corev1.SecretTypeTLS, Data: pair,
	}
	if err := objects.Create(ctx, secret); apierrors.IsAlreadyExists(err) {
		if err := objects.Update(ctx, secret); err != nil {
			t.Fatal(err)
		}
	} else if err != nil {
		t.Fatal(err)
	}
	if err := unstructured.SetNestedSlice(issued.Object, []interface{}{map[string]interface{}{
		"type": "Ready", "status": "True", "reason": "Ready", "message": "test issued", "lastTransitionTime": time.Now().UTC().Format(time.RFC3339),
	}}, "status", "conditions"); err != nil {
		t.Fatal(err)
	}
	if err := objects.Status().Update(ctx, issued); err != nil {
		t.Fatal(err)
	}
}

func ensureIssued(t *testing.T, requests requester.Requester, request requester.Request) requester.Issued {
	t.Helper()
	var issued requester.Issued
	eventually(t, "issuance for "+request.Owner.Name, func() (bool, string) {
		credential, err := requests.Ensure(t.Context(), request)
		if err != nil {
			t.Fatal(err)
		}
		issued, _ = credential.(requester.Issued)
		return issued.ID != "", fmt.Sprintf("%+v", credential)
	})
	return issued
}

func leaf(t *testing.T, ca *transporttesting.CA, name string) *x509.Certificate {
	t.Helper()
	var secret corev1.Secret
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: name}, &secret); err != nil {
		t.Fatal(err)
	}
	pair, err := tls.X509KeyPair(secret.Data[corev1.TLSCertKey], secret.Data[corev1.TLSPrivateKeyKey])
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	return parsed
}

func newResolver(t *testing.T) *resolver.Resolver {
	t.Helper()
	resolves, err := resolver.New(objects, []string{testNamespace}, &resolver.Identities{PolicyNamespace: testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	return resolves
}

// An owner's request is issued: the record's status names a fresh ID, its URI and a Secret, the
// Certificate asks the workload issuer for exactly that URI, and the requester sees it only once
// the Secret holds the key pair.
func TestARequestIsIssued(t *testing.T) {
	ca := transporttesting.NewCA(t)
	requests := newRequester(t)
	request := requester.Request{Owner: owner("test-issued"), Subject: requester.ServiceAccount{Name: subjectAccount}}
	if _, err := requests.Ensure(t.Context(), request); err != nil {
		t.Fatal(err)
	}
	record := observed(t, "test-issued")
	id := record.Status.CredentialID
	if !validCredentialID(id) || record.Status.URI != "spiffe://agentplane/"+testNamespace+"/test-issued/"+id ||
		record.Status.SecretName != "svid-test-issued-"+id || record.Status.AcceptedBinding != nil {
		t.Fatalf("status %+v", record.Status)
	}
	if credential, err := requests.Ensure(t.Context(), request); err != nil || credential.(requester.Pending).Reason != "CertificatePending" {
		t.Fatalf("before cert-manager: %+v, %v", credential, err)
	}
	issued, err := certificate(t, record.Status.SecretName)
	if err != nil {
		t.Fatal(err)
	}
	spec, _, _ := unstructured.NestedMap(issued.Object, "spec")
	if uris := spec["uris"].([]interface{}); len(uris) != 1 || uris[0] != record.Status.URI ||
		spec["issuerRef"].(map[string]interface{})["name"] != testIssuer || !metav1.IsControlledBy(issued, record) ||
		issued.GetLabels()[RecordUIDLabel] != string(record.UID) || issued.GetLabels()[CredentialIDLabel] != id {
		t.Fatalf("Certificate %+v", issued.Object)
	}
	issue(t, ca, record)
	if got := ensureIssued(t, requests, request); got != (requester.Issued{ID: id, URI: record.Status.URI, SecretName: record.Status.SecretName}) {
		t.Fatalf("issued %+v", got)
	}
}

// A credential ID lives exactly as long as its accepted binding: the first binding accepts the ID
// the incarnation was started with, and a new one is minted only when the binding leaves it.
// Superseded Certificates and their Secrets go once the new generation is issued.
func TestAFreshIDOnlyWhenTheBindingChanges(t *testing.T) {
	ctx := t.Context()
	ca := transporttesting.NewCA(t)
	requests := newRequester(t)
	resolves := newResolver(t)
	request := requester.Request{Owner: owner("test-rotation"), Subject: requester.ServiceAccount{Name: subjectAccount}}
	if _, err := requests.Ensure(ctx, request); err != nil {
		t.Fatal(err)
	}
	issue(t, ca, observed(t, "test-rotation"))
	started := ensureIssued(t, requests, request)

	firstPod := types.UID("11111111-1111-4111-8111-111111111111")
	request.Binding = requester.Pod{Name: "test-pod", UID: firstPod}
	accepted := ensureIssued(t, requests, request)
	if accepted.ID != started.ID || !accepted.Accepted {
		t.Fatalf("the first binding did not accept the ID it was started with: %+v then %+v", started, accepted)
	}
	firstLeaf := leaf(t, ca, accepted.SecretName)
	if peer, err := resolves.Resolve(ctx, firstLeaf); err != nil || peer.Binding.PodUID != firstPod {
		t.Fatalf("resolved %+v, %v", peer, err)
	}

	// cert-manager renewing the key under the same Certificate keeps the ID.
	issue(t, ca, get(t, "test-rotation"))
	if again := ensureIssued(t, requests, request); again != accepted {
		t.Fatalf("renewal changed the credential: %+v then %+v", accepted, again)
	}

	// Clearing the binding revokes at once and rotates.
	request.Binding = nil
	if _, err := requests.Ensure(ctx, request); err != nil {
		t.Fatal(err)
	}
	cleared := observed(t, "test-rotation")
	if cleared.Status.CredentialID == accepted.ID || cleared.Status.AcceptedBinding != nil {
		t.Fatalf("clearing the binding kept the ID: %+v", cleared.Status)
	}
	if _, err := resolves.Resolve(ctx, firstLeaf); err == nil {
		t.Fatal("the cleared incarnation's certificate still resolves")
	}
	issue(t, ca, cleared)
	restarted := ensureIssued(t, requests, request)

	// The old generation goes once the new one is issued.
	eventually(t, "the superseded Certificate and Secret to go", func() (bool, string) {
		_, certificateErr := certificate(t, accepted.SecretName)
		secretErr := objects.Get(ctx, types.NamespacedName{Namespace: testNamespace, Name: accepted.SecretName}, &corev1.Secret{})
		return apierrors.IsNotFound(certificateErr) && apierrors.IsNotFound(secretErr), fmt.Sprintf("%v, %v", certificateErr, secretErr)
	})
	if _, err := certificate(t, restarted.SecretName); err != nil {
		t.Fatalf("the current Certificate went too: %v", err)
	}

	// Replacing the binding with another Pod rotates too, and the new Pod accepts the fresh ID.
	request.Binding = requester.Pod{Name: "test-pod", UID: "22222222-2222-4222-8222-222222222222"}
	if accepted := ensureIssued(t, requests, request); accepted.ID != restarted.ID || !accepted.Accepted {
		t.Fatalf("binding after a fresh ID: %+v", accepted)
	}
	request.Binding = requester.Pod{Name: "test-pod", UID: "33333333-3333-4333-8333-333333333333"}
	if _, err := requests.Ensure(ctx, request); err != nil {
		t.Fatal(err)
	}
	replaced := observed(t, "test-rotation")
	if replaced.Status.CredentialID == restarted.ID || replaced.Status.AcceptedBinding == nil || replaced.Status.AcceptedBinding.PodUID != "33333333-3333-4333-8333-333333333333" {
		t.Fatalf("replacing the binding: %+v", replaced.Status)
	}
}

// An ownerless box mounts its issued Secret before it can bind the Pod. Accepting that first
// binding must keep the mounted Secret; replacing or clearing an accepted binding must rotate it.
func TestAnOwnerlessBoxKeepsItsMountedSecretUntilTheBindingChanges(t *testing.T) {
	ctx := t.Context()
	ca := transporttesting.NewCA(t)
	requests := newRequester(t)
	resolves := newResolver(t)
	request := requester.Request{
		Owner:   requester.Owner{Namespace: testNamespace, Name: "test-ownerless-box", Holder: requester.Unowned{Labels: map[string]string{"test-caller": "first"}}},
		Subject: requester.ServiceAccount{Name: subjectAccount},
	}
	if _, err := requests.Ensure(ctx, request); err != nil {
		t.Fatal(err)
	}
	issue(t, ca, observed(t, request.Owner.Name))
	started := ensureIssued(t, requests, request)
	mountedLeaf := leaf(t, ca, started.SecretName)

	firstPod := types.UID("55555555-5555-4555-8555-555555555555")
	request.Binding = requester.Pod{Name: "test-box-pod", UID: firstPod}
	if _, err := requests.Ensure(ctx, request); err != nil {
		t.Fatal(err)
	}
	bound := observed(t, request.Owner.Name)
	if bound.Status.CredentialID != started.ID || bound.Status.SecretName != started.SecretName {
		t.Fatalf("first binding changed the mounted credential: %+v then %+v", started, bound.Status)
	}
	accepted := ensureIssued(t, requests, request)
	if !accepted.Accepted {
		t.Fatalf("first Pod did not accept its mounted credential: %+v", accepted)
	}
	if peer, err := resolves.Resolve(ctx, mountedLeaf); err != nil || peer.Binding.PodUID != firstPod {
		t.Fatalf("the mounted credential resolved %+v, %v", peer, err)
	}
	if err := objects.Get(ctx, types.NamespacedName{Namespace: testNamespace, Name: started.SecretName}, &corev1.Secret{}); err != nil {
		t.Fatalf("the first Pod's mounted Secret disappeared: %v", err)
	}

	request.Binding = requester.Pod{Name: "test-box-pod", UID: "66666666-6666-4666-8666-666666666666"}
	if _, err := requests.Ensure(ctx, request); err != nil {
		t.Fatal(err)
	}
	replaced := observed(t, request.Owner.Name)
	if replaced.Status.CredentialID == started.ID || replaced.Status.SecretName == started.SecretName {
		t.Fatalf("replacement Pod reused the old credential: %+v", replaced.Status)
	}
	if _, err := resolves.Resolve(ctx, mountedLeaf); err == nil {
		t.Fatal("the previous Pod's mounted credential still resolves")
	}
	issue(t, ca, replaced)
	replacement := ensureIssued(t, requests, request)
	replacementLeaf := leaf(t, ca, replacement.SecretName)
	eventually(t, "the superseded mounted Secret to go", func() (bool, string) {
		err := objects.Get(ctx, types.NamespacedName{Namespace: testNamespace, Name: started.SecretName}, &corev1.Secret{})
		return apierrors.IsNotFound(err), fmt.Sprint(err)
	})

	request.Binding = nil
	if _, err := requests.Ensure(ctx, request); err != nil {
		t.Fatal(err)
	}
	cleared := observed(t, request.Owner.Name)
	if cleared.Status.CredentialID == replacement.ID || cleared.Status.SecretName == replacement.SecretName || cleared.Status.SecretName == started.SecretName {
		t.Fatalf("clearing the binding reused an earlier Secret: %+v", cleared.Status)
	}
	if _, err := resolves.Resolve(ctx, replacementLeaf); err == nil {
		t.Fatal("clearing the binding left the replacement Pod's credential valid")
	}
	issue(t, ca, cleared)
	restarted := ensureIssued(t, requests, request)
	request.Binding = requester.Pod{Name: "test-box-pod", UID: "77777777-7777-4777-8777-777777777777"}
	if rebound := ensureIssued(t, requests, request); rebound.ID != restarted.ID || rebound.SecretName != restarted.SecretName || !rebound.Accepted {
		t.Fatalf("the next Pod's first binding changed its mounted credential: %+v then %+v", restarted, rebound)
	}
}

// A standing workload has no owner and no binding: its ID and Secret name never change.
func TestAStandingWorkloadKeepsItsCredential(t *testing.T) {
	ctx := t.Context()
	ca := transporttesting.NewCA(t)
	record := &workloadapi.WorkloadCredential{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "test-standing"},
		Spec:       workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{ServiceAccount: subjectAccount}},
	}
	if err := objects.Create(ctx, record); err != nil {
		t.Fatal(err)
	}
	issued := observed(t, "test-standing")
	if issued.Status.SecretName != "svid-test-standing" {
		t.Fatalf("standing Secret name %q", issued.Status.SecretName)
	}
	issue(t, ca, issued)
	issue(t, ca, get(t, "test-standing"))
	eventually(t, "the standing record to be Ready", func() (bool, string) {
		current := get(t, "test-standing")
		ready := current.Status.Conditions
		return len(ready) == 1 && ready[0].Status == metav1.ConditionTrue && current.Status.CredentialID == issued.Status.CredentialID, fmt.Sprintf("%+v", current.Status)
	})
	if _, err := newResolver(t).Resolve(ctx, leaf(t, ca, "svid-test-standing")); err != nil {
		t.Fatal(err)
	}
}

// Deleting the record is emergency revocation: the next lookup refuses the old ID, and the
// Secret goes once the garbage collector, here the test, removes its Certificate.
func TestRevokingDeletesTheRecordAndItsSecret(t *testing.T) {
	ctx := t.Context()
	ca := transporttesting.NewCA(t)
	requests := newRequester(t)
	request := requester.Request{Owner: owner("test-revoked"), Subject: requester.ServiceAccount{Name: subjectAccount}}
	if _, err := requests.Ensure(ctx, request); err != nil {
		t.Fatal(err)
	}
	issue(t, ca, observed(t, "test-revoked"))
	ensureIssued(t, requests, request)
	request.Binding = requester.Pod{Name: "test-pod", UID: "44444444-4444-4444-8444-444444444444"}
	accepted := ensureIssued(t, requests, request)
	revokedLeaf := leaf(t, ca, accepted.SecretName)
	resolves := newResolver(t)
	if _, err := resolves.Resolve(ctx, revokedLeaf); err != nil {
		t.Fatal(err)
	}

	if err := requests.Revoke(ctx, request.Owner); err != nil {
		t.Fatal(err)
	}
	if _, err := resolves.Resolve(ctx, revokedLeaf); err == nil {
		t.Fatal("a revoked credential still resolves")
	}
	issued, err := certificate(t, accepted.SecretName)
	if err != nil {
		t.Fatal(err)
	}
	if err := objects.Delete(ctx, issued); err != nil {
		t.Fatal(err)
	}
	eventually(t, "the revoked record's Secret to go", func() (bool, string) {
		err := objects.Get(ctx, types.NamespacedName{Namespace: testNamespace, Name: accepted.SecretName}, &corev1.Secret{})
		return apierrors.IsNotFound(err), fmt.Sprint(err)
	})
}
