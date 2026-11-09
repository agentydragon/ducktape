package sandboxcontroller

import (
	"context"
	"testing"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/issuer"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/requester"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/apimachinery/pkg/types"
	clienttesting "k8s.io/client-go/testing"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const runnerCredentialTestSecret = "svid-test-0123456789abcdef0123456789abcdef"

// addCredentialKinds registers WorkloadCredentials and cert-manager's Certificates, which these
// tests issue with the real issuer.
func addCredentialKinds(t *testing.T, scheme *runtime.Scheme) {
	t.Helper()
	if err := workloadapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	certificate := issuer.NewCertificate().GroupVersionKind()
	scheme.AddKnownTypeWithName(certificate, &unstructured.Unstructured{})
	scheme.AddKnownTypeWithName(certificate.GroupVersion().WithKind("CertificateList"), &unstructured.UnstructuredList{})
	metav1.AddToGroupVersion(scheme, certificate.GroupVersion())
}

// fakeCertManagerCreate creates obj; a Certificate is created Ready, with the Secret cert-manager
// would write, labelled from its secretTemplate.
func fakeCertManagerCreate(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
	certificate, ok := obj.(*unstructured.Unstructured)
	if !ok || certificate.GroupVersionKind() != issuer.NewCertificate().GroupVersionKind() {
		return c.Create(ctx, obj, opts...)
	}
	certificate.Object["status"] = map[string]interface{}{"conditions": []interface{}{map[string]interface{}{"type": "Ready", "status": "True"}}}
	if err := c.Create(ctx, obj, opts...); err != nil {
		return err
	}
	labels, _, err := unstructured.NestedStringMap(certificate.Object, "spec", "secretTemplate", "labels")
	if err != nil {
		return err
	}
	return c.Create(ctx, &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: certificate.GetNamespace(), Name: certificate.GetName(), Labels: labels,
			Annotations: map[string]string{"cert-manager.io/certificate-name": certificate.GetName()},
		},
		Type: corev1.SecretTypeTLS,
		Data: map[string][]byte{"tls.crt": []byte("test-cert"), "tls.key": []byte("test-key")},
	})
}

func testRequester(t *testing.T, c client.Client) requester.Requester {
	t.Helper()
	requests, err := requester.New(c, c)
	if err != nil {
		t.Fatal(err)
	}
	return requests
}

// issueCredential runs the Policy Controller's issuer on the Sandbox's record until it settles.
func issueCredential(t *testing.T, c client.Client, sandbox client.ObjectKey) {
	t.Helper()
	issues := &issuer.Reconciler{Client: c, APIReader: c, Issuer: "test-workload-issuer"}
	for range 3 {
		if _, err := issues.Reconcile(context.Background(), reconcile.Request{NamespacedName: sandbox}); err != nil {
			t.Fatal(err)
		}
	}
}

// requestIssued asks for the Sandbox's credential, as the controller does, and issues it.
func requestIssued(t *testing.T, r *Reconciler, sandbox *sandboxapi.Sandbox, binding requester.Binding) requester.Issued {
	t.Helper()
	for range 2 {
		issued, pending, err := r.requestCredential(context.Background(), sandbox, requester.ServiceAccount{Name: sandbox.Name}, binding)
		if err != nil {
			t.Fatal(err)
		}
		if pending == nil {
			return issued
		}
		issueCredential(t, r.Client, client.ObjectKeyFromObject(sandbox))
	}
	t.Fatal("the credential stayed pending after issuance")
	return requester.Issued{}
}

func newCredentialReconciler(t *testing.T, objects ...client.Object) (*Reconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, sandboxapi.AddToScheme, kubevirtv1.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	addCredentialKinds(t, scheme)
	// A tracker without managed fields: the fake client's default one cannot convert KubeVirt's
	// uint64 fields.
	tracker := clienttesting.NewObjectTracker(scheme, serializer.NewCodecFactory(scheme).UniversalDecoder())
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjectTracker(tracker).
		WithStatusSubresource(&sandboxapi.Sandbox{}, &workloadapi.WorkloadCredential{}).
		WithObjects(objects...).
		WithInterceptorFuncs(interceptor.Funcs{Create: fakeCertManagerCreate}).Build()
	return &Reconciler{Client: c, Objects: c, Credentials: testRequester(t, c)}, c
}

func credentialTestSandbox() *sandboxapi.Sandbox {
	return &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: "sandboxes", Name: "test", UID: "sandbox-uid"}, Spec: sandboxapi.SandboxSpec{Running: true}}
}

func readRecord(t *testing.T, c client.Client, sandbox *sandboxapi.Sandbox) *workloadapi.WorkloadCredential {
	t.Helper()
	var record workloadapi.WorkloadCredential
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(sandbox), &record); err != nil {
		t.Fatal(err)
	}
	return &record
}

func TestPodRunnerTLSVolumeUsesCredentialSpecificSecret(t *testing.T) {
	template := &sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Namespace: "sandboxes", Name: "environment-runtime"}}
	podSpec := corev1.PodSpec{
		Containers: []corev1.Container{
			{Name: EnvironmentRuntimeContainer},
			{Name: "workload"},
		},
		InitContainers: []corev1.Container{{Name: EnvironmentRuntimeEgressSidecar}},
	}
	if blocked := withRunnerTLS(template, &podSpec, runnerCredentialTestSecret); blocked != nil {
		t.Fatalf("runner TLS projection: %v", blocked)
	}
	volume := podSpec.Volumes[0]
	if volume.Name != EnvironmentRuntimeTLSVolumeName || volume.Projected == nil || volume.Projected.DefaultMode == nil || *volume.Projected.DefaultMode != 0440 {
		t.Fatalf("runner TLS volume permissions/source = %#v", volume)
	}
	if got := volume.Projected.Sources[0].Secret.Name; got != runnerCredentialTestSecret {
		t.Fatalf("Pod mounts Secret %q, want the issued credential's %q", got, runnerCredentialTestSecret)
	}
	if got := volume.Projected.Sources[1].ConfigMap.Name; got != RunnerTrustBundleName {
		t.Fatalf("Pod trusts ConfigMap %q, want %q", got, RunnerTrustBundleName)
	}
	if got := volume.Projected.Sources[1].ConfigMap.Items[0].Path; got != "ca.crt" {
		t.Fatalf("trust bundle path = %q, want ca.crt", got)
	}
	// The Gateway's client SVID is verified against the workload CA alone, beside the cluster bundle.
	if got := volume.Projected.Sources[2].ConfigMap; got == nil || got.Name != RunnerWorkloadTrustBundleName || len(got.Items) != 1 || got.Items[0].Path != RunnerWorkloadTrustFile {
		t.Fatalf("workload trust projection = %#v, want %s as %s", got, RunnerWorkloadTrustBundleName, RunnerWorkloadTrustFile)
	}
	if len(podSpec.Containers[0].VolumeMounts) != 1 || podSpec.Containers[0].VolumeMounts[0].Name != volume.Name {
		t.Fatalf("runtime did not receive exactly the runner TLS mount: %#v", podSpec.Containers[0].VolumeMounts)
	}
	if len(podSpec.InitContainers[0].VolumeMounts) != 1 || podSpec.InitContainers[0].VolumeMounts[0].Name != volume.Name {
		t.Fatalf("egress sidecar did not receive exactly the runner TLS mount: %#v", podSpec.InitContainers[0].VolumeMounts)
	}
	if len(podSpec.Containers[1].VolumeMounts) != 0 {
		t.Fatalf("workload received the runner TLS volume: %#v", podSpec.Containers[1].VolumeMounts)
	}
}

func TestPodRunnerTLSWithoutEgressSidecarMountsOnlyRuntime(t *testing.T) {
	template := &sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Namespace: "sandboxes", Name: "runtime-only"}}
	podSpec := corev1.PodSpec{Containers: []corev1.Container{{Name: EnvironmentRuntimeContainer}, {Name: "workload"}}}
	if blocked := withRunnerTLS(template, &podSpec, runnerCredentialTestSecret); blocked != nil {
		t.Fatalf("runtime-only template was rejected: %v", blocked)
	}
	if len(podSpec.Volumes) != 1 || podSpec.Volumes[0].Name != EnvironmentRuntimeTLSVolumeName {
		t.Fatalf("runtime TLS volume = %+v", podSpec.Volumes)
	}
	if len(podSpec.Containers[0].VolumeMounts) != 1 || podSpec.Containers[0].VolumeMounts[0].Name != EnvironmentRuntimeTLSVolumeName {
		t.Fatalf("Runtime TLS mounts = %+v", podSpec.Containers[0].VolumeMounts)
	}
	if len(podSpec.Containers[1].VolumeMounts) != 0 {
		t.Fatalf("workload received a TLS mount: %+v", podSpec.Containers[1].VolumeMounts)
	}
}

func TestPodRunnerTLSRejectsOtherContainersClaimingReservedMount(t *testing.T) {
	template := &sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Namespace: "sandboxes", Name: "environment-runtime"}}
	cases := []struct {
		name          string
		container     corev1.Container
		initContainer bool
	}{
		{
			name: "workload mounts injected credential volume at another path",
			container: corev1.Container{Name: "workload", VolumeMounts: []corev1.VolumeMount{{
				Name: EnvironmentRuntimeTLSVolumeName, MountPath: "/stolen",
			}}},
		},
		{
			name: "init container claims reserved mount path",
			container: corev1.Container{Name: "setup", VolumeMounts: []corev1.VolumeMount{{
				Name: "template-volume", MountPath: EnvironmentRuntimeTLSMountPath,
			}}},
			initContainer: true,
		},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			podSpec := corev1.PodSpec{Containers: []corev1.Container{{Name: EnvironmentRuntimeContainer}}}
			if test.initContainer {
				podSpec.InitContainers = []corev1.Container{test.container}
			} else {
				podSpec.Containers = append(podSpec.Containers, test.container)
			}
			blocked := withRunnerTLS(template, &podSpec, runnerCredentialTestSecret)
			if blocked == nil || blocked.reason != "TemplateInvalid" {
				t.Fatalf("reserved runner TLS mount was accepted: %v", blocked)
			}
			if len(podSpec.Volumes) != 0 || len(podSpec.Containers[0].VolumeMounts) != 0 {
				t.Fatalf("rejected template was mutated before validation: volumes=%v runtime mounts=%v", podSpec.Volumes, podSpec.Containers[0].VolumeMounts)
			}
		})
	}
}

// credentialTestVM is the Sandbox's VM, its template carrying credential id and acceptedVMI recorded
// as accepted for it.
func credentialTestVM(sandbox *sandboxapi.Sandbox, id, acceptedVMI string, strategy kubevirtv1.VirtualMachineRunStrategy) *kubevirtv1.VirtualMachine {
	return &kubevirtv1.VirtualMachine{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: sandbox.Namespace, Name: sandbox.Name, UID: "vm-uid",
			Annotations: map[string]string{RunnerCredentialIDAnnotation: id, RunnerCredentialVMIUIDAnnotation: acceptedVMI},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: "Sandbox", Name: sandbox.Name, UID: sandbox.UID, Controller: new(true),
			}},
		},
		Spec: kubevirtv1.VirtualMachineSpec{RunStrategy: &strategy},
	}
}

func credentialTestVMI(sandbox *sandboxapi.Sandbox, uid types.UID, id string) *kubevirtv1.VirtualMachineInstance {
	return &kubevirtv1.VirtualMachineInstance{ObjectMeta: metav1.ObjectMeta{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: uid,
		Labels: map[string]string{sandboxapi.EnvironmentRuntimeCredentialLabel: id},
	}}
}

// acceptVMI has the record accept vmiUID, as an earlier run of the controller would have.
func acceptVMI(t *testing.T, r *Reconciler, sandbox *sandboxapi.Sandbox, vmiUID types.UID) requester.Issued {
	t.Helper()
	requestIssued(t, r, sandbox, nil)
	issued := requestIssued(t, r, sandbox, requester.VMI{UID: vmiUID})
	if !issued.Accepted {
		t.Fatalf("the record did not accept VMI %s: %+v", vmiUID, issued)
	}
	return issued
}

func TestAcceptedVMRunnerCredentialPublishesIncarnation(t *testing.T) {
	ctx := context.Background()
	sandbox := credentialTestSandbox()
	r, c := newCredentialReconciler(t, sandbox)
	issued := acceptVMI(t, r, sandbox, "vmi-current")
	vm := credentialTestVM(sandbox, issued.ID, "vmi-current", kubevirtv1.RunStrategyAlways)
	vmi := credentialTestVMI(sandbox, "vmi-current", issued.ID)
	launcher := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: sandbox.Namespace, Name: "virt-launcher-test", UID: "launcher-current"}}
	for _, object := range []client.Object{vm, vmi} {
		if err := c.Create(ctx, object); err != nil {
			t.Fatal(err)
		}
	}
	w := workload{vm: vm, vmi: vmi, launcher: launcher}
	if _, pending, err := r.ensureVMRunnerCredential(ctx, sandbox, w, true); err != nil || pending == nil {
		t.Fatalf("naming the launcher should wait for the issuer: pending=%v err=%v", pending, err)
	}
	issueCredential(t, c, client.ObjectKeyFromObject(sandbox))
	w, pending, err := r.ensureVMRunnerCredential(ctx, sandbox, w, true)
	if err != nil || pending != nil {
		t.Fatalf("accepted VMI: pending=%v err=%v", pending, err)
	}
	if !w.runnerCredentialAccepted || !w.runnerIncarnationPublished || w.credential.ID != issued.ID {
		t.Fatalf("matching, issued VMI was not published: workload=%#v", w)
	}
	if pod := readRecord(t, c, sandbox).Spec.Pod; pod == nil || pod.UID != launcher.UID || pod.Name != launcher.Name {
		t.Fatalf("the record names %+v as the caller's Pod, want the launcher", pod)
	}
}

// A VMI KubeVirt recreated from the accepted one's template is never accepted: the VM halts and the
// record unbinds, which revokes the credential the template carries and rotates the ID.
func TestUnexpectedVMReplacementRotatesCredentialAndHaltsVM(t *testing.T) {
	ctx := context.Background()
	sandbox := credentialTestSandbox()
	r, c := newCredentialReconciler(t, sandbox)
	issued := acceptVMI(t, r, sandbox, "vmi-old")
	vm := credentialTestVM(sandbox, issued.ID, "vmi-old", kubevirtv1.RunStrategyAlways)
	vmi := credentialTestVMI(sandbox, "vmi-new", issued.ID)
	for _, object := range []client.Object{vm, vmi} {
		if err := c.Create(ctx, object); err != nil {
			t.Fatal(err)
		}
	}
	w, pending, err := r.ensureVMRunnerCredential(ctx, sandbox, workload{vm: vm, vmi: vmi}, true)
	if err != nil {
		t.Fatal(err)
	}
	if pending == nil || pending.reason != "RunnerCredentialMismatch" {
		t.Fatalf("unexpected VMI replacement must be fenced: %v", pending)
	}
	if w.runnerCredentialAccepted || w.runnerIncarnationPublished {
		t.Fatal("unexpected replacement VMI must not be accepted or published")
	}
	var gotVM kubevirtv1.VirtualMachine
	if err := c.Get(ctx, client.ObjectKeyFromObject(vm), &gotVM); err != nil {
		t.Fatal(err)
	}
	if gotVM.Spec.RunStrategy == nil || *gotVM.Spec.RunStrategy != kubevirtv1.RunStrategyHalted {
		t.Fatalf("unexpected replacement VM was not halted: runStrategy=%v", gotVM.Spec.RunStrategy)
	}
	if gotVM.Annotations[RunnerCredentialVMIUIDAnnotation] != "" || gotVM.Annotations[RunnerCredentialLastVMIUIDAnnotation] != "vmi-old" {
		t.Fatalf("VM credential binding lost its accepted incarnation fence: %#v", gotVM.Annotations)
	}
	if record := readRecord(t, c, sandbox); record.Spec.Binding != nil {
		t.Fatalf("the record still binds %+v", record.Spec.Binding)
	}
	issueCredential(t, c, client.ObjectKeyFromObject(sandbox))
	if record := readRecord(t, c, sandbox); record.Status.CredentialID == issued.ID || record.Status.AcceptedBinding != nil {
		t.Fatalf("the replaced VMI's credential was not rotated: %+v", record.Status)
	}
}

// A stopped VM whose last VMI was accepted restarts only with a fresh credential, written into its
// template before it runs.
func TestFinalizerRotatesStoppedVMRunnerCredentialBeforeRestart(t *testing.T) {
	ctx := context.Background()
	sandbox := credentialTestSandbox()
	r, c := newCredentialReconciler(t, sandbox)
	old := acceptVMI(t, r, sandbox, "vmi-old")
	vm := credentialTestVM(sandbox, old.ID, "vmi-old", kubevirtv1.RunStrategyHalted)
	if err := c.Create(ctx, vm); err != nil {
		t.Fatal(err)
	}
	w, pending, err := r.ensureVMRunnerCredential(ctx, sandbox, workload{vm: vm}, true)
	if err != nil || pending == nil {
		t.Fatalf("a fresh VM credential should gate restart while it is issued: pending=%v err=%v", pending, err)
	}
	if _, gate, err := r.converge(ctx, sandbox, w, true, pending); err != nil || gate == nil {
		t.Fatalf("converge discarded the pending credential gate: gate=%v err=%v", gate, err)
	}
	var gotVM kubevirtv1.VirtualMachine
	if err := c.Get(ctx, client.ObjectKeyFromObject(vm), &gotVM); err != nil {
		t.Fatal(err)
	}
	if gotVM.Spec.RunStrategy == nil || *gotVM.Spec.RunStrategy != kubevirtv1.RunStrategyHalted {
		t.Fatal("VM started while its replacement credential was pending")
	}

	issueCredential(t, c, client.ObjectKeyFromObject(sandbox))
	w, pending, err = r.ensureVMRunnerCredential(ctx, sandbox, workload{vm: &gotVM}, true)
	if err != nil || pending != nil {
		t.Fatalf("issued VM credential: pending=%v err=%v", pending, err)
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(vm), &gotVM); err != nil {
		t.Fatal(err)
	}
	newID := gotVM.Annotations[RunnerCredentialIDAnnotation]
	if newID == old.ID || newID != w.credential.ID || !validRunnerCredentialID(newID) {
		t.Fatalf("restart did not rotate the old VMI's credential: %q, workload %q", newID, w.credential.ID)
	}
	if gotVM.Annotations[RunnerCredentialVMIUIDAnnotation] != "" || gotVM.Annotations[RunnerCredentialLastVMIUIDAnnotation] != "vmi-old" {
		t.Fatalf("stopped VM lost the prior VMI fence: %#v", gotVM.Annotations)
	}
	if w.runnerCredentialAccepted || w.runnerIncarnationPublished {
		t.Fatalf("new VM identity was accepted before a VMI bound it: %#v", w)
	}
}

func TestDeletingVMRetainsOnlyPreviouslyPublishedLauncher(t *testing.T) {
	deletionTimestamp := metav1.NewTime(testNow)
	for _, tc := range []struct {
		name       string
		launcherID string
		wantLive   bool
	}{
		{name: "current", launcherID: "launcher-current", wantLive: true},
		{name: "replacement", launcherID: "launcher-replacement", wantLive: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			launcher := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "virt-launcher-test", UID: types.UID(tc.launcherID)}}
			sandbox := credentialTestSandbox()
			sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: launcher.Name, UID: "launcher-current"}
			vm := credentialTestVM(sandbox, "0123456789abcdef0123456789abcdef", "", kubevirtv1.RunStrategyAlways)
			vm.DeletionTimestamp, vm.Finalizers = &deletionTimestamp, []string{"test.example/finalizer"}
			r, c := newCredentialReconciler(t, sandbox, vm.DeepCopy())
			w, pending, err := r.ensureVMRunnerCredential(context.Background(), sandbox, workload{vm: vm, launcher: launcher}, true)
			if err != nil || pending != nil {
				t.Fatalf("deleting VM should not wait on credential mutation: pending=%v err=%v", pending, err)
			}
			if w.runnerIncarnationPublished != tc.wantLive {
				t.Fatalf("published replacement launcher = %v, want %v", w.runnerIncarnationPublished, tc.wantLive)
			}
			if err := c.Get(context.Background(), client.ObjectKeyFromObject(sandbox), &workloadapi.WorkloadCredential{}); !apierrors.IsNotFound(err) {
				t.Fatalf("a deleting VM requested a credential: %v", err)
			}
		})
	}
}

func TestPodCredentialMismatchDoesNotPublishReplacementDuringFinalization(t *testing.T) {
	ctx := context.Background()
	sandbox := credentialTestSandbox()
	sandbox.Status.RunnerCredential = &sandboxapi.RunnerCredential{Kind: sandboxapi.RunnerCredentialKindPod, PodUID: "pod-old"}
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: sandbox.Name, UID: "pod-old"}
	pod := runnerCredentialTestPod("pod-replacement", "0123456789abcdef0123456789abcdef")
	r, c := newCredentialReconciler(t, sandbox, pod)
	w, pending, err := r.ensureRunnerCredential(ctx, sandbox, workload{pod: pod}, true)
	if err != nil || pending == nil || pending.reason != "RunnerCredentialMismatch" {
		t.Fatalf("replacement Pod should remain fenced: pending=%v err=%v", pending, err)
	}
	if w.runnerIncarnationPublished {
		t.Fatal("replacement Pod inherited the prior Pod UID's published route")
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(pod), &corev1.Pod{}); !apierrors.IsNotFound(err) {
		t.Fatalf("stale replacement Pod was not deleted: %v", err)
	}
}

// A Pod started with a credential the record no longer issues is replaced, never accepted.
func TestPodWithARotatedCredentialIsReplaced(t *testing.T) {
	ctx := context.Background()
	sandbox := credentialTestSandbox()
	r, c := newCredentialReconciler(t, sandbox)
	started := requestIssued(t, r, sandbox, nil)
	first := runnerCredentialTestPod("pod-first", started.ID)
	if err := c.Create(ctx, first); err != nil {
		t.Fatal(err)
	}
	w, pending, err := r.ensureRunnerCredential(ctx, getSandboxObject(t, c, sandbox), workload{pod: first}, true)
	if err == nil && pending != nil {
		issueCredential(t, c, client.ObjectKeyFromObject(sandbox))
		w, pending, err = r.ensureRunnerCredential(ctx, getSandboxObject(t, c, sandbox), workload{pod: first}, true)
	}
	if err != nil || pending != nil || !w.runnerCredentialAccepted || w.credential.ID != started.ID {
		t.Fatalf("the first Pod was not accepted with the ID it started with: %+v, pending=%v err=%v", w, pending, err)
	}
	if bound := getRunnerCredential(t, c, sandbox); bound.PodUID != "pod-first" {
		t.Fatalf("Sandbox accepted %+v", bound)
	}

	// The first Pod is gone; a stale replacement still carries its ID.
	if err := c.Delete(ctx, first); err != nil {
		t.Fatal(err)
	}
	if _, _, err := r.ensureRunnerCredential(ctx, getSandboxObject(t, c, sandbox), workload{}, true); err != nil {
		t.Fatal(err)
	}
	issueCredential(t, c, client.ObjectKeyFromObject(sandbox))
	stale := runnerCredentialTestPod("pod-stale", started.ID)
	if err := c.Create(ctx, stale); err != nil {
		t.Fatal(err)
	}
	if _, pending, err := r.ensureRunnerCredential(ctx, getSandboxObject(t, c, sandbox), workload{pod: stale}, true); err != nil || pending == nil || pending.reason != "RunnerCredentialMismatch" {
		t.Fatalf("a Pod with the revoked ID was not fenced: pending=%v err=%v", pending, err)
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(stale), &corev1.Pod{}); !apierrors.IsNotFound(err) {
		t.Fatalf("the stale Pod was not deleted: %v", err)
	}
}

func runnerCredentialTestPod(uid types.UID, id string) *corev1.Pod {
	return &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: "sandboxes", Name: "test", UID: uid,
		Labels: map[string]string{sandboxapi.EnvironmentRuntimeCredentialLabel: id},
	}}
}

func getSandboxObject(t *testing.T, c client.Client, sandbox *sandboxapi.Sandbox) *sandboxapi.Sandbox {
	t.Helper()
	var current sandboxapi.Sandbox
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(sandbox), &current); err != nil {
		t.Fatal(err)
	}
	return &current
}

func getRunnerCredential(t *testing.T, c client.Client, sandbox *sandboxapi.Sandbox) sandboxapi.RunnerCredential {
	t.Helper()
	return runnerCredential(getSandboxObject(t, c, sandbox))
}

func TestStatusBindingIgnoresRunnerCredentialAnnotations(t *testing.T) {
	ctx := context.Background()
	// A Sandbox writer holding patch but not sandboxes/status sets the VM credential annotations on
	// the Sandbox, pointing at another generation.
	sandbox := credentialTestSandbox()
	sandbox.Annotations = map[string]string{
		RunnerCredentialIDAnnotation:         "fedcba9876543210fedcba9876543210",
		RunnerCredentialLastVMIUIDAnnotation: "vmi-other",
	}
	r, c := newCredentialReconciler(t, sandbox)
	issued := requestIssued(t, r, sandbox, nil)
	pod := runnerCredentialTestPod("pod-current", issued.ID)
	if err := c.Create(ctx, pod); err != nil {
		t.Fatal(err)
	}
	requestIssued(t, r, sandbox, requester.Pod{Name: pod.Name, UID: pod.UID})
	current := getSandboxObject(t, c, sandbox)
	current.Status.RunnerCredential = &sandboxapi.RunnerCredential{Kind: sandboxapi.RunnerCredentialKindPod, PodUID: "pod-current"}
	if err := c.Status().Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	w, pending, err := r.ensureRunnerCredential(ctx, current, workload{pod: pod}, true)
	if err != nil || pending != nil || !w.runnerCredentialAccepted {
		t.Fatalf("annotations unbound the runner credential: pending=%v err=%v", pending, err)
	}
	if got := getRunnerCredential(t, c, sandbox); got != *current.Status.RunnerCredential {
		t.Fatalf("status binding changed to %#v", got)
	}
}
