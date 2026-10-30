package sandboxcontroller

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/discovery"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	kubevirtv1 "kubevirt.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

func eventually(t *testing.T, check func() bool) {
	t.Helper()
	if err := wait.PollUntilContextTimeout(context.Background(), 10*time.Millisecond, 30*time.Second, true,
		func(context.Context) (bool, error) { return check(), nil }); err != nil {
		t.Fatal(err)
	}
}

func withRunnerCertificateCRD(t *testing.T) kubeapiserver.Option {
	t.Helper()
	directory := t.TempDir()
	manifest := `apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: certificates.cert-manager.io
spec:
  group: cert-manager.io
  names:
    plural: certificates
    singular: certificate
    kind: Certificate
    listKind: CertificateList
  scope: Namespaced
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
          properties:
            spec:
              type: object
              x-kubernetes-preserve-unknown-fields: true
            status:
              type: object
              x-kubernetes-preserve-unknown-fields: true
      subresources:
        status: {}
`
	if err := os.WriteFile(filepath.Join(directory, "certificate.yaml"), []byte(manifest), 0o600); err != nil {
		t.Fatal(err)
	}
	return func(environment *envtest.Environment) {
		environment.CRDDirectoryPaths = append(environment.CRDDirectoryPaths, directory)
	}
}

func runFakeCertificateSigner(ctx context.Context, objects client.Client, namespace string) error {
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		certificates := &unstructured.UnstructuredList{}
		certificates.SetGroupVersionKind(runnerCertificateGVK.GroupVersion().WithKind("CertificateList"))
		if err := objects.List(ctx, certificates, client.InNamespace(namespace)); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		for i := range certificates.Items {
			certificate := &certificates.Items[i]
			if !certificateReady(certificate) {
				certificate.Object["status"] = map[string]interface{}{"conditions": []interface{}{map[string]interface{}{
					"type": "Ready", "status": "True",
				}}}
				if err := objects.Status().Update(ctx, certificate); err != nil && !apierrors.IsConflict(err) {
					if ctx.Err() != nil {
						return nil
					}
					return err
				}
			}
			labels := map[string]string{
				sandboxapi.SandboxLabel:          certificate.GetLabels()[sandboxapi.SandboxLabel],
				sandboxapi.RunnerCredentialLabel: certificate.GetLabels()[sandboxapi.RunnerCredentialLabel],
			}
			secret := &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: certificate.GetNamespace(), Name: certificate.GetName(), Labels: labels,
					Annotations: map[string]string{runnerCertificateNameAnnotation: certificate.GetName()},
				},
				Type: corev1.SecretTypeTLS,
				Data: map[string][]byte{"tls.crt": []byte("test-cert"), "tls.key": []byte("test-key")},
			}
			if err := objects.Create(ctx, secret); err != nil && !apierrors.IsAlreadyExists(err) {
				if ctx.Err() != nil {
					return nil
				}
				return err
			}
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}

// Exercise a real API server without KubeVirt and real SSA field ownership and manager watches.
func TestChildApplyOwnershipAndDriftWithoutKubeVirt(t *testing.T) {
	ctrl.SetLogger(logr.FromSlogHandler(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug})))
	// Kubernetes defaults selector-less headless Services to dual-stack. Configure both
	// families so their defaulted fields also pass validation on later drift updates.
	kube, err := kubeapiserver.Start(
		kubeapiserver.WithAPIServerFlag("service-cluster-ip-range", "10.0.0.0/24,fd00::/108"),
		withRunnerCertificateCRD(t),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := kube.Stop(); err != nil {
			t.Error(err)
		}
	})
	config := kube.Config
	discoveryClient, err := discovery.NewDiscoveryClientForConfig(config)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := discoveryClient.ServerResourcesForGroupVersion(kubevirtv1.GroupVersion.String()); !apierrors.IsNotFound(err) {
		t.Fatalf("envtest unexpectedly serves KubeVirt: %v", err)
	}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{
		clientgoscheme.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme, kubevirtv1.AddToScheme,
	} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	const ns = "child-ssa"
	byObject, err := CacheByObject(false)
	if err != nil {
		t.Fatal(err)
	}
	mgr, err := ctrl.NewManager(config, ctrl.Options{
		Scheme: scheme, Metrics: metricsserver.Options{BindAddress: "0"},
		Cache: cache.Options{DefaultNamespaces: map[string]cache.Config{ns: {}}, ByObject: byObject},
	})
	if err != nil {
		t.Fatal(err)
	}
	objects, err := client.New(config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	r := &Reconciler{
		Client: mgr.GetClient(), Objects: mgr.GetAPIReader(), KubeVirt: false, Leases: mgr.GetClient(),
		RunnerCertificateIssuer: "test-issuer", Now: time.Now,
	}
	if err := r.SetupWithManager(mgr); err != nil {
		t.Fatal(err)
	}
	if err := objects.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatal(err)
	}
	template := &sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Name: "runner", Namespace: ns}, Spec: sandboxapi.SandboxTemplateSpec{
		Pod: &corev1.PodTemplateSpec{Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: RunnerContainer, Image: "example.invalid/runner:v1"}}}},
		StateVolume: sandboxapi.StateVolume{Name: "state", Layout: sandboxapi.VolumeLayout{Type: sandboxapi.DirectoryTree}, Claim: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources:   corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("1Gi")}},
		}},
	}}
	if err := objects.Create(ctx, template); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- mgr.Start(ctx) }()
	t.Cleanup(func() {
		cancel()
		if err := <-done; err != nil {
			t.Error(err)
		}
	})
	signerDone := make(chan error, 1)
	go func() { signerDone <- runFakeCertificateSigner(ctx, objects, ns) }()
	t.Cleanup(func() {
		cancel()
		if err := <-signerDone; err != nil {
			t.Error(err)
		}
	})
	key := types.NamespacedName{Namespace: ns, Name: "sandbox"}
	foreign := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: ns}}
	if err := objects.Create(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: ns}, Spec: sandboxapi.SandboxSpec{
		Template: template.Name, Running: true, PolicySets: []string{"desired-grant"}, Bootstrap: "echo desired",
	}}
	if err := objects.Create(ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool {
		var current sandboxapi.Sandbox
		if objects.Get(ctx, key, &current) != nil {
			return false
		}
		condition := meta.FindStatusCondition(current.Status.Conditions, sandboxapi.ConditionRunning)
		return condition != nil && condition.Reason == "ServiceAccountConflict"
	})
	if err := objects.Get(ctx, key, &corev1.Pod{}); !apierrors.IsNotFound(err) {
		t.Fatalf("foreign ServiceAccount allowed a Pod: %v", err)
	}
	if err := objects.Delete(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	if err := objects.Get(ctx, key, sandbox); err != nil {
		t.Fatal(err)
	}
	sandbox.Annotations = map[string]string{"retry": "after-foreign-account-removal"}
	if err := objects.Update(ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool { return objects.Get(ctx, key, &corev1.Pod{}) == nil })
	// The app and Runner Gateway hold patch on Sandboxes but not sandboxes/status: neither the main
	// resource's status nor the old binding annotations can rebind the runner credential.
	var pod corev1.Pod
	var bound sandboxapi.RunnerCredential
	eventually(t, func() bool {
		var current sandboxapi.Sandbox
		if objects.Get(ctx, key, &pod) != nil || objects.Get(ctx, key, &current) != nil || current.Status.RunnerCredential == nil {
			return false
		}
		bound = *current.Status.RunnerCredential
		return bound.PodUID == string(pod.UID)
	})
	const otherID = "fedcba9876543210fedcba9876543210"
	if err := objects.Patch(ctx, sandbox, client.RawPatch(types.MergePatchType, []byte(`{
		"metadata": {"annotations": {"agentplane.allegedly.works/runner-credential": "`+otherID+`", "agentplane.allegedly.works/runner-credential-kind": "pod"}},
		"status": {"runnerCredential": {"id": "`+otherID+`", "kind": "Pod"}}
	}`))); err != nil {
		t.Fatal(err)
	}
	// The controller strips the annotations when it reconciles the patch.
	eventually(t, func() bool {
		return objects.Get(ctx, key, sandbox) == nil && sandbox.Annotations[RunnerCredentialIDAnnotation] == ""
	})
	var unchanged corev1.Pod
	if err := objects.Get(ctx, key, &unchanged); err != nil || unchanged.UID != pod.UID {
		t.Fatalf("spec writer's patch replaced the bound Pod: %v", err)
	}
	if sandbox.Status.RunnerCredential == nil || *sandbox.Status.RunnerCredential != bound {
		t.Fatalf("spec writer's patch changed the binding to %#v, want %#v", sandbox.Status.RunnerCredential, bound)
	}
	var account corev1.ServiceAccount
	if err := objects.Get(ctx, key, &account); err != nil {
		t.Fatal(err)
	}
	if err := objects.Patch(ctx, &account, client.RawPatch(types.MergePatchType, []byte(`{"metadata":{"labels":{"agentplane.allegedly.works/use-action-service":"false","unrelated":"keep"}}}`))); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool {
		return objects.Get(ctx, key, &account) == nil && account.Labels[ActionCallerLabel] == "true" && account.Labels["unrelated"] == "keep"
	})
	var launch corev1.ConfigMap
	launchKey := types.NamespacedName{Namespace: ns, Name: LaunchConfigMapName(key.Name)}
	if err := objects.Get(ctx, launchKey, &launch); err != nil {
		t.Fatal(err)
	}
	wantLaunch := launch.Data[LaunchFile]
	launch.Data = map[string]string{LaunchFile: "wrong", "unrelated": "keep"}
	if err := objects.Update(ctx, &launch); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool {
		return objects.Get(ctx, launchKey, &launch) == nil && launch.Data[LaunchFile] == wantLaunch && launch.Data["unrelated"] == "keep"
	})
	version := launch.ResourceVersion
	if blocked, err := r.ensureLaunch(ctx, sandbox); blocked != nil || err != nil {
		t.Fatalf("unchanged launch apply: %v, %v", blocked, err)
	}
	if err := objects.Get(ctx, launchKey, &launch); err != nil || launch.ResourceVersion != version {
		t.Fatalf("unchanged apply wrote ConfigMap: version %s -> %s, error %v", version, launch.ResourceVersion, err)
	}
	deletedUID := launch.UID
	if err := objects.Delete(ctx, &launch); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool {
		return objects.Get(ctx, launchKey, &launch) == nil && launch.UID != deletedUID && launch.Data[LaunchFile] == wantLaunch
	})
	var binding policyapi.PolicyBinding
	if err := objects.Get(ctx, key, &binding); err != nil {
		t.Fatal(err)
	}
	if err := objects.Patch(ctx, &binding, client.RawPatch(types.MergePatchType, []byte(`{"spec":{"policySets":["wrong"],"expiresAt":"2099-01-01T00:00:00Z"}}`))); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool {
		return objects.Get(ctx, key, &binding) == nil && len(binding.Spec.PolicySets) == 1 && binding.Spec.PolicySets[0] == "desired-grant" && binding.Spec.ExpiresAt == nil
	})
	var service corev1.Service
	serviceKey := types.NamespacedName{Namespace: ns, Name: key.Name + sandboxapi.ControlServiceSuffix}
	if err := objects.Get(ctx, serviceKey, &service); err != nil {
		t.Fatal(err)
	}
	if err := objects.Patch(ctx, &service, client.RawPatch(types.MergePatchType, []byte(`{"spec":{"selector":{"wrong":"pod"},"ports":[{"port":8080,"targetPort":1234,"protocol":"TCP","name":"wrong"}]}}`))); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool {
		return objects.Get(ctx, serviceKey, &service) == nil && len(service.Spec.Selector) == 0 && len(service.Spec.Ports) == 1 && service.Spec.Ports[0].Name == sandboxapi.ControlServicePortName && service.Spec.Ports[0].TargetPort.IntVal == sandboxapi.ControlServicePort
	})
	if err := objects.Patch(ctx, sandbox, client.RawPatch(types.MergePatchType, []byte(`{"spec":{"policySets":[]}}`))); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool { return apierrors.IsNotFound(objects.Get(ctx, key, &policyapi.PolicyBinding{})) })
	for _, test := range []struct {
		name, reason string
		foreign      client.Object
	}{
		{"foreign-launch", "ConfigMapConflict", &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "foreign-launch-launch", Namespace: ns}}},
		{"foreign-binding", "PolicyBindingConflict", &policyapi.PolicyBinding{
			ObjectMeta: metav1.ObjectMeta{Name: "foreign-binding", Namespace: ns},
			Spec:       policyapi.PolicyBindingSpec{Subject: policyapi.ServiceAccountRef{Namespace: ns, Name: "foreign-binding"}, PolicySets: []string{"foreign-grant"}},
		}},
		{"foreign-service", "ServiceConflict", &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "foreign-service-control", Namespace: ns}, Spec: corev1.ServiceSpec{ClusterIP: corev1.ClusterIPNone}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := objects.Create(ctx, test.foreign); err != nil {
				t.Fatal(err)
			}
			other := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: test.name, Namespace: ns}, Spec: sandboxapi.SandboxSpec{Template: template.Name, Running: true, PolicySets: []string{"desired-grant"}}}
			if err := objects.Create(ctx, other); err != nil {
				t.Fatal(err)
			}
			otherKey := client.ObjectKeyFromObject(other)
			eventually(t, func() bool {
				if objects.Get(ctx, otherKey, other) != nil {
					return false
				}
				condition := meta.FindStatusCondition(other.Status.Conditions, sandboxapi.ConditionRunning)
				return condition != nil && condition.Reason == test.reason
			})
			if err := objects.Get(ctx, otherKey, &corev1.Pod{}); !apierrors.IsNotFound(err) {
				t.Fatalf("foreign child allowed a Pod: %v", err)
			}
		})
	}
	t.Run("template-change-enqueues-sandbox", func(t *testing.T) {
		changed := template.DeepCopy()
		changed.ObjectMeta = metav1.ObjectMeta{Name: "repaired-template", Namespace: ns}
		changed.Spec.Pod.Spec.Containers[0].Name = "missing-runner"
		if err := objects.Create(ctx, changed); err != nil {
			t.Fatal(err)
		}
		other := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: "template-watch", Namespace: ns}, Spec: sandboxapi.SandboxSpec{Template: changed.Name, Running: true}}
		if err := objects.Create(ctx, other); err != nil {
			t.Fatal(err)
		}
		otherKey := client.ObjectKeyFromObject(other)
		eventually(t, func() bool {
			if objects.Get(ctx, otherKey, other) != nil {
				return false
			}
			condition := meta.FindStatusCondition(other.Status.Conditions, sandboxapi.ConditionRunning)
			return condition != nil && condition.Reason == "TemplateInvalid"
		})
		changed.Spec.Pod.Spec.Containers[0].Name = RunnerContainer
		if err := objects.Update(ctx, changed); err != nil {
			t.Fatal(err)
		}
		eventually(t, func() bool { return objects.Get(ctx, otherKey, &corev1.Pod{}) == nil })
	})
	t.Run("cache-holds-only-labeled-objects", func(t *testing.T) {
		for _, test := range []struct {
			kind      string
			unlabeled client.Object
			labeled   client.Object
		}{
			{
				"Secret",
				&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "cache-unlabeled-secret", Namespace: ns}},
				&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "cache-labeled-secret", Namespace: ns, Labels: map[string]string{sandboxapi.SandboxLabel: "no-such-sandbox"}}},
			},
			{
				"ConfigMap",
				&corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "cache-unlabeled-configmap", Namespace: ns}},
				&corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "cache-labeled-configmap", Namespace: ns, Labels: map[string]string{sandboxapi.SandboxLabel: "no-such-sandbox"}}},
			},
		} {
			t.Run(test.kind, func(t *testing.T) {
				// The unlabeled object is created first, so once the labeled one is cached the
				// informer has seen the earlier event too.
				if err := objects.Create(ctx, test.unlabeled); err != nil {
					t.Fatal(err)
				}
				if err := objects.Create(ctx, test.labeled); err != nil {
					t.Fatal(err)
				}
				eventually(t, func() bool {
					return mgr.GetCache().Get(ctx, client.ObjectKeyFromObject(test.labeled), test.labeled.DeepCopyObject().(client.Object)) == nil
				})
				if err := mgr.GetCache().Get(ctx, client.ObjectKeyFromObject(test.unlabeled), test.unlabeled.DeepCopyObject().(client.Object)); !apierrors.IsNotFound(err) {
					t.Fatalf("cache holds the unlabeled %s: %v", test.kind, err)
				}
			})
		}
	})
	vmTemplate := template.DeepCopy()
	vmTemplate.ObjectMeta = metav1.ObjectMeta{Namespace: ns, Name: "vm-runner"}
	vmTemplate.Spec.Pod = nil
	vmTemplate.Spec.VM = &kubevirtv1.VirtualMachineSpec{Template: &kubevirtv1.VirtualMachineInstanceTemplateSpec{}}
	if err := objects.Create(ctx, vmTemplate); err != nil {
		t.Fatal(err)
	}
	vmSandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: "vm-without-api", Namespace: ns},
		Spec:       sandboxapi.SandboxSpec{Template: vmTemplate.Name, Running: true},
	}
	if err := objects.Create(ctx, vmSandbox); err != nil {
		t.Fatal(err)
	}
	vmKey := client.ObjectKeyFromObject(vmSandbox)
	eventually(t, func() bool {
		if objects.Get(ctx, vmKey, vmSandbox) != nil {
			return false
		}
		condition := meta.FindStatusCondition(vmSandbox.Status.Conditions, sandboxapi.ConditionRunning)
		return condition != nil && condition.Reason == "KubeVirtMissing"
	})
}
