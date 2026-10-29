package sandboxcontroller

import (
	"context"
	"testing"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const runnerCredentialTestID = "0123456789abcdef0123456789abcdef"

func runnerCredentialTestScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := kubevirtv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	certificateGV := schema.GroupVersion{Group: runnerCertificateGroup, Version: runnerCertificateVersion}
	scheme.AddKnownTypeWithName(certificateGV.WithKind(runnerCertificateKind), &unstructured.Unstructured{})
	scheme.AddKnownTypeWithName(certificateGV.WithKind("CertificateList"), &unstructured.UnstructuredList{})
	metav1.AddToGroupVersion(scheme, certificateGV)
	return scheme
}

func TestRunnerCertificateSpecMatchesCertManagerDefaults(t *testing.T) {
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: "sandboxes", Name: "test", UID: "sandbox-uid"}}
	expected, found, err := unstructured.NestedMap(runnerCertificateObject(sandbox, "runner-issuer", runnerCredentialTestID).Object, "spec")
	if err != nil || !found {
		t.Fatalf("expected certificate spec: found=%v err=%v", found, err)
	}

	withDefaults := runtime.DeepCopyJSON(expected)
	withDefaults["duration"] = "24h0m0s"
	withDefaults["renewBefore"] = "8h0m0s"
	withDefaults["isCA"] = false
	withDefaults["revisionHistoryLimit"] = int64(1)
	privateKey := withDefaults["privateKey"].(map[string]interface{})
	privateKey["encoding"] = "PKCS8"
	if !runnerCertificateSpecMatches(withDefaults, expected) {
		t.Fatal("cert-manager defaults or canonical durations rejected an otherwise matching certificate")
	}

	tests := map[string]func(map[string]interface{}){
		"URI": func(spec map[string]interface{}) { spec["uris"] = []interface{}{"spiffe://agentplane/runner/other"} },
		"issuer": func(spec map[string]interface{}) {
			spec["issuerRef"].(map[string]interface{})["name"] = "other-issuer"
		},
		"secret name": func(spec map[string]interface{}) { spec["secretName"] = "shared-runner-secret" },
		"extra usage": func(spec map[string]interface{}) {
			spec["usages"] = []interface{}{"digital signature", "server auth", "code signing"}
		},
		"rotation policy": func(spec map[string]interface{}) {
			spec["privateKey"].(map[string]interface{})["rotationPolicy"] = "Never"
		},
		"extra SAN field": func(spec map[string]interface{}) { spec["dnsNames"] = []interface{}{"other.example"} },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			actual := runtime.DeepCopyJSON(expected)
			mutate(actual)
			if runnerCertificateSpecMatches(actual, expected) {
				t.Fatal("certificate with a different identity or key policy was accepted")
			}
		})
	}
}

func TestEnsureRunnerCertificateAcceptsOnlyReadyMatchingSecret(t *testing.T) {
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: "sandboxes", Name: "test", UID: "sandbox-uid"}}
	certificate := runnerCertificateObject(sandbox, "runner-issuer", runnerCredentialTestID)
	certificate.SetUID(types.UID("certificate-uid"))
	certificate.Object["status"] = map[string]interface{}{"conditions": []interface{}{map[string]interface{}{"type": "Ready", "status": "True"}}}
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: sandbox.Namespace,
			Name:      RunnerCertificateSecretName(runnerCredentialTestID),
			Labels: map[string]string{
				sandboxapi.SandboxLabel:          sandbox.Name,
				sandboxapi.RunnerCredentialLabel: runnerCredentialTestID,
			},
			Annotations: map[string]string{runnerCertificateNameAnnotation: RunnerCertificateSecretName(runnerCredentialTestID)},
		},
		Type: corev1.SecretTypeTLS,
		Data: map[string][]byte{"tls.crt": []byte("certificate"), "tls.key": []byte("private key")},
	}
	c := fake.NewClientBuilder().WithScheme(runnerCredentialTestScheme(t)).WithObjects(certificate, secret).Build()
	r := &Reconciler{Client: c, Objects: c, RunnerCertificateIssuer: "runner-issuer"}
	if pending, err := r.ensureRunnerCertificate(context.Background(), sandbox, runnerCredentialTestID); err != nil || pending != nil {
		t.Fatalf("matching Ready Certificate and Secret: pending=%v err=%v", pending, err)
	}

	secret.Labels[sandboxapi.RunnerCredentialLabel] = "fedcba9876543210fedcba9876543210"
	if err := c.Update(context.Background(), secret); err != nil {
		t.Fatal(err)
	}
	pending, err := r.ensureRunnerCertificate(context.Background(), sandbox, runnerCredentialTestID)
	if err != nil || pending == nil || pending.reason != "CertificateSecretConflict" {
		t.Fatalf("mismatched credential label must block mount: pending=%v err=%v", pending, err)
	}
}

func TestPodRunnerTLSVolumeUsesCredentialSpecificSecret(t *testing.T) {
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: "sandboxes", Name: "test", UID: "sandbox-uid"}}
	template := &sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Namespace: sandbox.Namespace, Name: "runner"}}
	podSpec := corev1.PodSpec{Containers: []corev1.Container{{Name: RunnerContainer}}}
	if blocked := withRunnerTLS(sandbox, template, &podSpec, runnerCredentialTestID); blocked != nil {
		t.Fatalf("runner TLS projection: %v", blocked)
	}
	volume := podSpec.Volumes[0]
	if volume.Name != RunnerTLSVolumeName || volume.Projected == nil || volume.Projected.DefaultMode == nil || *volume.Projected.DefaultMode != 0440 {
		t.Fatalf("runner TLS volume permissions/source = %#v", volume)
	}
	if got := volume.Projected.Sources[0].Secret.Name; got != RunnerCertificateSecretName(runnerCredentialTestID) {
		t.Fatalf("Pod mounts Secret %q, want unique credential Secret %q", got, RunnerCertificateSecretName(runnerCredentialTestID))
	}
	if got := volume.Projected.Sources[1].ConfigMap.Name; got != RunnerTrustBundleName {
		t.Fatalf("Pod trusts ConfigMap %q, want %q", got, RunnerTrustBundleName)
	}
	if got := volume.Projected.Sources[1].ConfigMap.Items[0].Path; got != "ca.crt" {
		t.Fatalf("trust bundle path = %q, want ca.crt", got)
	}
}

func TestUnexpectedVMReplacementRotatesCredentialAndHaltsVM(t *testing.T) {
	ctx := context.Background()
	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "sandboxes", Name: "test", UID: "sandbox-uid",
			Annotations: map[string]string{
				RunnerCredentialIDAnnotation:         runnerCredentialTestID,
				RunnerCredentialKindAnnotation:       "vm",
				RunnerCredentialLastVMIUIDAnnotation: "vmi-old",
				RunnerCredentialAcceptedIDAnnotation: runnerCredentialTestID,
			},
		},
		Spec: sandboxapi.SandboxSpec{Running: true},
	}
	vm := &kubevirtv1.VirtualMachine{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: sandbox.Namespace, Name: sandbox.Name, UID: "vm-uid",
			Annotations: map[string]string{
				RunnerCredentialIDAnnotation:     runnerCredentialTestID,
				RunnerCredentialVMIUIDAnnotation: "vmi-old",
			},
			OwnerReferences: []metav1.OwnerReference{{APIVersion: sandboxapi.GroupVersion.String(), Kind: "Sandbox", Name: sandbox.Name, UID: sandbox.UID, Controller: runnerCredentialTestPtr(true)}},
		},
		Spec: kubevirtv1.VirtualMachineSpec{RunStrategy: runnerCredentialTestPtr(kubevirtv1.RunStrategyAlways)},
	}
	vmi := &kubevirtv1.VirtualMachineInstance{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: sandbox.Namespace, Name: sandbox.Name, UID: "vmi-new",
			Labels: map[string]string{
				sandboxapi.RunnerCredentialLabel: runnerCredentialTestID,
			},
			OwnerReferences: []metav1.OwnerReference{{APIVersion: kubevirtv1.GroupVersion.String(), Kind: "VirtualMachine", Name: vm.Name, UID: vm.UID, Controller: runnerCredentialTestPtr(true)}},
		},
	}
	c := fake.NewClientBuilder().WithScheme(runnerCredentialTestScheme(t)).WithObjects(sandbox, vm, vmi).Build()
	r := &Reconciler{Client: c, Objects: c, RunnerCertificateIssuer: "runner-issuer"}
	w, pending, err := r.ensureVMRunnerCredential(ctx, sandbox, workload{vm: vm, vmi: vmi}, true)
	if err != nil {
		t.Fatal(err)
	}
	if pending == nil {
		t.Fatal("unexpected VMI replacement must be fenced while its fresh certificate is issued")
	}
	if w.runnerCredentialAccepted || w.runnerIncarnationPublished {
		t.Fatal("unexpected replacement VMI must not be accepted or published")
	}
	var gotVM kubevirtv1.VirtualMachine
	if err := c.Get(ctx, client.ObjectKeyFromObject(vm), &gotVM); err != nil {
		t.Fatal(err)
	}
	newID := gotVM.Annotations[RunnerCredentialIDAnnotation]
	if !validRunnerCredentialID(newID) || newID == runnerCredentialTestID {
		t.Fatalf("replacement did not get a fresh credential ID: %q", newID)
	}
	if gotVM.Spec.RunStrategy == nil || *gotVM.Spec.RunStrategy != kubevirtv1.RunStrategyHalted {
		t.Fatalf("unexpected replacement VM was not halted: runStrategy=%v", gotVM.Spec.RunStrategy)
	}
	if gotVM.Annotations[RunnerCredentialVMIUIDAnnotation] != "" || gotVM.Annotations[RunnerCredentialLastVMIUIDAnnotation] != "vmi-old" {
		t.Fatalf("VM credential binding lost its accepted incarnation fence: %#v", gotVM.Annotations)
	}
	var gotSandbox sandboxapi.Sandbox
	if err := c.Get(ctx, client.ObjectKeyFromObject(sandbox), &gotSandbox); err != nil {
		t.Fatal(err)
	}
	if gotSandbox.Annotations[RunnerCredentialIDAnnotation] != newID {
		t.Fatalf("Sandbox binding %q does not match replacement credential %q", gotSandbox.Annotations[RunnerCredentialIDAnnotation], newID)
	}
	var newCertificate unstructured.Unstructured
	newCertificate.SetGroupVersionKind(runnerCertificateGVK)
	if err := c.Get(ctx, client.ObjectKey{Namespace: sandbox.Namespace, Name: RunnerCertificateSecretName(newID)}, &newCertificate); err != nil {
		t.Fatalf("fresh Certificate was not requested for the replacement: %v", err)
	}
	uris, found, err := unstructured.NestedStringSlice(newCertificate.Object, "spec", "uris")
	if err != nil || !found || len(uris) != 1 || uris[0] != runnerCertificateURI(sandbox, newID) {
		t.Fatalf("replacement Certificate has wrong immutable URI: uris=%v found=%v err=%v", uris, found, err)
	}
}

func TestAcceptedVMRunnerCredentialPublishesIncarnation(t *testing.T) {
	ctx := context.Background()
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Namespace: "sandboxes", Name: "test", UID: "sandbox-uid",
		Annotations: map[string]string{
			RunnerCredentialIDAnnotation:         runnerCredentialTestID,
			RunnerCredentialKindAnnotation:       "vm",
			RunnerCredentialLastVMIUIDAnnotation: "vmi-current",
			RunnerCredentialAcceptedIDAnnotation: runnerCredentialTestID,
		},
	}}
	vm := &kubevirtv1.VirtualMachine{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: sandbox.Namespace, Name: sandbox.Name, UID: "vm-uid",
			Annotations: map[string]string{
				RunnerCredentialIDAnnotation:     runnerCredentialTestID,
				RunnerCredentialVMIUIDAnnotation: "vmi-current",
			},
		},
	}
	vmi := &kubevirtv1.VirtualMachineInstance{ObjectMeta: metav1.ObjectMeta{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: "vmi-current",
		Labels: map[string]string{
			sandboxapi.RunnerCredentialLabel: runnerCredentialTestID,
		},
	}}
	certificate := runnerCertificateObject(sandbox, "runner-issuer", runnerCredentialTestID)
	certificate.SetUID(types.UID("certificate-uid"))
	certificate.Object["status"] = map[string]interface{}{"conditions": []interface{}{map[string]interface{}{"type": "Ready", "status": "True"}}}
	c := fake.NewClientBuilder().WithScheme(runnerCredentialTestScheme(t)).WithObjects(sandbox, vm, vmi, certificate, runnerCredentialTestSecret(sandbox, runnerCredentialTestID)).Build()
	r := &Reconciler{Client: c, Objects: c, RunnerCertificateIssuer: "runner-issuer"}
	w, pending, err := r.ensureVMRunnerCredential(ctx, sandbox, workload{vm: vm, vmi: vmi}, true)
	if err != nil || pending != nil {
		t.Fatalf("accepted VMI: pending=%v err=%v", pending, err)
	}
	if !w.runnerCredentialAccepted || !w.runnerIncarnationPublished {
		t.Fatalf("matching, certificate-ready VMI was not published: workload=%#v", w)
	}
}

func TestFinalizerRotatesStoppedVMRunnerCredentialBeforeRestart(t *testing.T) {
	ctx := context.Background()
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Namespace: "sandboxes", Name: "test", UID: "sandbox-uid",
		Annotations: map[string]string{
			RunnerCredentialIDAnnotation:         runnerCredentialTestID,
			RunnerCredentialKindAnnotation:       "vm",
			RunnerCredentialLastVMIUIDAnnotation: "vmi-old",
			RunnerCredentialAcceptedIDAnnotation: runnerCredentialTestID,
		},
	}}
	halted := kubevirtv1.RunStrategyHalted
	vm := &kubevirtv1.VirtualMachine{ObjectMeta: metav1.ObjectMeta{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: "vm-uid",
		Annotations: map[string]string{
			RunnerCredentialIDAnnotation:     runnerCredentialTestID,
			RunnerCredentialVMIUIDAnnotation: "vmi-old",
		},
	}, Spec: kubevirtv1.VirtualMachineSpec{RunStrategy: &halted}}
	c := fake.NewClientBuilder().WithScheme(runnerCredentialTestScheme(t)).WithObjects(sandbox, vm).Build()
	r := &Reconciler{Client: c, Objects: c, RunnerCertificateIssuer: "runner-issuer"}
	w, pending, err := r.ensureVMRunnerCredential(ctx, sandbox, workload{vm: vm}, true)
	if err != nil || pending == nil {
		t.Fatalf("fresh VM credential should gate restart while cert-manager issues it: pending=%v err=%v", pending, err)
	}
	if w.runnerCredentialAccepted || w.runnerIncarnationPublished {
		t.Fatalf("new VM identity was accepted before a VMI bound it: %#v", w)
	}
	var gotVM kubevirtv1.VirtualMachine
	if err := c.Get(ctx, client.ObjectKeyFromObject(vm), &gotVM); err != nil {
		t.Fatal(err)
	}
	newID := gotVM.Annotations[RunnerCredentialIDAnnotation]
	if !validRunnerCredentialID(newID) || newID == runnerCredentialTestID {
		t.Fatalf("finalizer restart did not rotate the old VMI credential: %q", newID)
	}
	if gotVM.Annotations[RunnerCredentialVMIUIDAnnotation] != "" || gotVM.Annotations[RunnerCredentialLastVMIUIDAnnotation] != "vmi-old" {
		t.Fatalf("stopped VM lost the prior VMI fence: %#v", gotVM.Annotations)
	}
	if gotVM.Spec.RunStrategy == nil || *gotVM.Spec.RunStrategy != kubevirtv1.RunStrategyHalted {
		t.Fatalf("credential issuance started the VM before readiness: runStrategy=%v", gotVM.Spec.RunStrategy)
	}
	var gotSandbox sandboxapi.Sandbox
	if err := c.Get(ctx, client.ObjectKeyFromObject(sandbox), &gotSandbox); err != nil {
		t.Fatal(err)
	}
	if gotSandbox.Annotations[RunnerCredentialIDAnnotation] != newID {
		t.Fatalf("Sandbox credential %q does not match VM credential %q", gotSandbox.Annotations[RunnerCredentialIDAnnotation], newID)
	}
	_, gate, err := r.converge(ctx, &gotSandbox, w, true, pending)
	if err != nil {
		t.Fatal(err)
	}
	if gate == nil {
		t.Fatal("converge discarded the pending certificate gate")
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(vm), &gotVM); err != nil {
		t.Fatal(err)
	}
	if gotVM.Spec.RunStrategy == nil || *gotVM.Spec.RunStrategy != kubevirtv1.RunStrategyHalted {
		t.Fatal("VM started while its replacement credential was pending")
	}
}

func TestDeletingVMRetainsOnlyPreviouslyPublishedLauncher(t *testing.T) {
	deletionTimestamp := metav1.NewTime(testNow)
	vm := &kubevirtv1.VirtualMachine{ObjectMeta: metav1.ObjectMeta{
		Namespace: "sandboxes", Name: "test", UID: "vm-uid", DeletionTimestamp: &deletionTimestamp,
		Finalizers:  []string{"test.example/finalizer"},
		Annotations: map[string]string{RunnerCredentialIDAnnotation: runnerCredentialTestID},
	}}
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
			sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
				Namespace: "sandboxes", Name: "test", UID: "sandbox-uid",
				Annotations: map[string]string{RunnerCredentialIDAnnotation: runnerCredentialTestID},
			}}
			sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: launcher.Name, UID: "launcher-current"}
			c := fake.NewClientBuilder().WithScheme(runnerCredentialTestScheme(t)).WithObjects(sandbox, vm.DeepCopy()).Build()
			r := &Reconciler{Client: c, Objects: c, RunnerCertificateIssuer: "runner-issuer"}
			w, pending, err := r.ensureVMRunnerCredential(context.Background(), sandbox, workload{vm: vm.DeepCopy(), launcher: launcher}, true)
			if err != nil || pending != nil {
				t.Fatalf("deleting VM should not wait on credential mutation: pending=%v err=%v", pending, err)
			}
			if w.runnerIncarnationPublished != tc.wantLive {
				t.Fatalf("published replacement launcher = %v, want %v", w.runnerIncarnationPublished, tc.wantLive)
			}
			var gotVM kubevirtv1.VirtualMachine
			if err := c.Get(context.Background(), client.ObjectKeyFromObject(vm), &gotVM); err != nil {
				t.Fatal(err)
			}
			if gotVM.Annotations[RunnerCredentialIDAnnotation] != runnerCredentialTestID {
				t.Fatalf("deleting VM credential changed to %q", gotVM.Annotations[RunnerCredentialIDAnnotation])
			}
		})
	}
}

func TestPodCredentialMismatchDoesNotPublishReplacementDuringFinalization(t *testing.T) {
	ctx := context.Background()
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Namespace: "sandboxes", Name: "test", UID: "sandbox-uid",
		Annotations: map[string]string{
			RunnerCredentialIDAnnotation:     runnerCredentialTestID,
			RunnerCredentialKindAnnotation:   "pod",
			RunnerCredentialPodUIDAnnotation: "pod-old",
		},
	}}
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: sandbox.Name, UID: "pod-old"}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: "pod-replacement",
		Labels: map[string]string{
			sandboxapi.RunnerCredentialLabel: runnerCredentialTestID,
		},
	}}
	c := fake.NewClientBuilder().WithScheme(runnerCredentialTestScheme(t)).WithObjects(sandbox, pod).Build()
	r := &Reconciler{Client: c, Objects: c, RunnerCertificateIssuer: "runner-issuer"}
	w, pending, err := r.ensureRunnerCredential(ctx, sandbox, workload{pod: pod}, true)
	if err != nil || pending == nil || pending.reason != "RunnerCredentialMismatch" {
		t.Fatalf("replacement Pod should remain fenced: pending=%v err=%v", pending, err)
	}
	if w.runnerIncarnationPublished {
		t.Fatal("replacement Pod inherited the prior Pod UID's published route")
	}
	var gotPod corev1.Pod
	if err := c.Get(ctx, client.ObjectKeyFromObject(pod), &gotPod); !apierrors.IsNotFound(err) {
		t.Fatalf("stale replacement Pod was not deleted: %v", err)
	}
}

func TestDeleteOldRunnerCertificatesRemovesOnlyManagedStaleGeneration(t *testing.T) {
	ctx := context.Background()
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: "sandboxes", Name: "test", UID: "sandbox-uid"}}
	oldID := "fedcba9876543210fedcba9876543210"
	oldCertificate := runnerCertificateObject(sandbox, "runner-issuer", oldID)
	oldCertificate.SetUID(types.UID("old-certificate-uid"))
	activeID := runnerCredentialTestID
	oldSecret := runnerCredentialTestSecret(sandbox, oldID)
	activeSecret := runnerCredentialTestSecret(sandbox, activeID)
	unrelatedSecret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Namespace: sandbox.Namespace,
		Name:      "unrelated",
		Labels:    map[string]string{sandboxapi.SandboxLabel: sandbox.Name},
	}}
	c := fake.NewClientBuilder().WithScheme(runnerCredentialTestScheme(t)).WithObjects(oldCertificate, oldSecret, activeSecret, unrelatedSecret).Build()
	r := &Reconciler{Client: c, Objects: c}
	if pending, err := r.deleteOldRunnerCertificates(ctx, sandbox, activeID); err != nil || pending != nil {
		t.Fatalf("cleanup: pending=%v err=%v", pending, err)
	}
	for _, key := range []client.ObjectKey{
		{Namespace: sandbox.Namespace, Name: oldCertificate.GetName()},
		{Namespace: sandbox.Namespace, Name: oldSecret.Name},
	} {
		if key.Name == oldCertificate.GetName() {
			var cert unstructured.Unstructured
			cert.SetGroupVersionKind(runnerCertificateGVK)
			if err := c.Get(ctx, key, &cert); !apierrors.IsNotFound(err) {
				t.Errorf("stale Certificate %s lookup error = %v, want NotFound", key.Name, err)
			}
			continue
		}
		var got corev1.Secret
		if err := c.Get(ctx, key, &got); !apierrors.IsNotFound(err) {
			t.Errorf("stale Secret %s lookup error = %v, want NotFound", key.Name, err)
		}
	}
	for _, key := range []client.ObjectKey{
		{Namespace: sandbox.Namespace, Name: activeSecret.Name},
		{Namespace: sandbox.Namespace, Name: unrelatedSecret.Name},
	} {
		var got corev1.Secret
		if err := c.Get(ctx, key, &got); err != nil {
			t.Errorf("cleanup removed non-stale Secret %s: %v", key.Name, err)
		}
	}
}

func TestDeleteOldRunnerCertificatesPreservesForeignRunnerLikeSecret(t *testing.T) {
	ctx := context.Background()
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: "sandboxes", Name: "test", UID: "sandbox-uid"}}
	foreignID := "fedcba9876543210fedcba9876543210"
	foreignSecret := runnerCredentialTestSecret(sandbox, foreignID)
	foreignSecret.Annotations[runnerCertificateNameAnnotation] = "unrelated-certificate"
	c := fake.NewClientBuilder().WithScheme(runnerCredentialTestScheme(t)).WithObjects(foreignSecret).Build()
	r := &Reconciler{Client: c, Objects: c}

	pending, err := r.deleteOldRunnerCertificates(ctx, sandbox, runnerCredentialTestID)
	if err != nil || pending == nil || pending.reason != "CertificateSecretConflict" {
		t.Fatalf("foreign runner-like Secret should block cleanup: pending=%v err=%v", pending, err)
	}
	var got corev1.Secret
	key := client.ObjectKeyFromObject(foreignSecret)
	if err := c.Get(ctx, key, &got); err != nil {
		t.Fatalf("cleanup removed foreign runner-like Secret: %v", err)
	}
}

func runnerCredentialTestSecret(sandbox *sandboxapi.Sandbox, credentialID string) *corev1.Secret {
	name := RunnerCertificateSecretName(credentialID)
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: sandbox.Namespace,
			Name:      name,
			Labels: map[string]string{
				sandboxapi.SandboxLabel:          sandbox.Name,
				sandboxapi.RunnerCredentialLabel: credentialID,
			},
			Annotations: map[string]string{runnerCertificateNameAnnotation: name},
		},
		Type: corev1.SecretTypeTLS,
		Data: map[string][]byte{"tls.crt": []byte("certificate"), "tls.key": []byte("private key")},
	}
}

func runnerCredentialTestPtr[T any](v T) *T { return &v }
