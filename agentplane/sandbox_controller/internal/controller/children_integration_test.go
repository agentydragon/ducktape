package sandboxcontroller

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/issuer"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
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
	"k8s.io/client-go/rest"
	kubevirtv1 "kubevirt.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	ctrlconfig "sigs.k8s.io/controller-runtime/pkg/config"
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
		certificates.SetGroupVersionKind(issuer.NewCertificate().GroupVersionKind().GroupVersion().WithKind("CertificateList"))
		if err := objects.List(ctx, certificates, client.InNamespace(namespace)); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		for i := range certificates.Items {
			certificate := &certificates.Items[i]
			if conditions, _, _ := unstructured.NestedSlice(certificate.Object, "status", "conditions"); len(conditions) == 0 {
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
			// cert-manager copies the Secret's labels from the Certificate's secretTemplate.
			labels, _, err := unstructured.NestedStringMap(certificate.Object, "spec", "secretTemplate", "labels")
			if err != nil {
				return err
			}
			secret := &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: certificate.GetNamespace(), Name: certificate.GetName(), Labels: labels,
					Annotations: map[string]string{"cert-manager.io/certificate-name": certificate.GetName()},
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

func TestManagerCacheSyncNeedsOnlyGrantedSandboxWatchKinds(t *testing.T) {
	const ns = "sandbox-controller-watch-contract"
	kube, err := kubeapiserver.Start(kubeapiserver.WithAPIServerFlag("authorization-mode", "RBAC"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := kube.Stop(); err != nil {
			t.Error(err)
		}
	})
	ctx := context.Background()
	if _, err := kube.Clientset.CoreV1().Namespaces().Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	// The serving controller acts through WorkloadCredentials and has no use for either resource.
	if _, err := kube.Clientset.RbacV1().Roles(ns).Create(ctx, &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "sandbox-controller"},
		Rules: []rbacv1.PolicyRule{
			{APIGroups: []string{""}, Resources: []string{"pods", "persistentvolumeclaims", "configmaps", "services", "events"}, Verbs: []string{"get", "list", "watch"}},
			{APIGroups: []string{"coordination.k8s.io"}, Resources: []string{"leases"}, Verbs: []string{"get", "list", "watch"}},
			{APIGroups: []string{sandboxapi.GroupVersion.Group}, Resources: []string{"sandboxes", "sandboxtemplates"}, Verbs: []string{"get", "list", "watch"}},
			{APIGroups: []string{policyapi.SchemeGroupVersion.Group}, Resources: []string{"identities", "workloadcredentials"}, Verbs: []string{"get", "list", "watch"}},
		},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := kube.Clientset.RbacV1().RoleBindings(ns).Create(ctx, &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "sandbox-controller"},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: "sandbox-controller"},
		Subjects:   []rbacv1.Subject{{Kind: rbacv1.UserKind, APIGroup: rbacv1.GroupName, Name: kubeapiserver.User}},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	controllerConfig := rest.CopyConfig(kube.Config)
	controllerConfig.BearerToken = kube.UserToken
	controllerConfig.BearerTokenFile = ""
	controllerConfig.Username, controllerConfig.Password = "", ""
	controllerConfig.TLSClientConfig.CertFile, controllerConfig.TLSClientConfig.KeyFile = "", ""
	controllerConfig.TLSClientConfig.CertData, controllerConfig.TLSClientConfig.KeyData = nil, nil
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{
		clientgoscheme.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme, workloadapi.AddToScheme, kubevirtv1.AddToScheme,
	} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	byObject, err := CacheByObject(false)
	if err != nil {
		t.Fatal(err)
	}
	mgr, err := ctrl.NewManager(controllerConfig, ctrl.Options{
		Scheme: scheme, Metrics: metricsserver.Options{BindAddress: "0"}, HealthProbeBindAddress: "0",
		// Both tests create the Sandbox controller in the same process.
		Controller: ctrlconfig.Controller{SkipNameValidation: new(true)},
		Cache:      cache.Options{DefaultNamespaces: map[string]cache.Config{ns: {}}, ByObject: byObject},
	})
	if err != nil {
		t.Fatal(err)
	}
	r := &Reconciler{Client: mgr.GetClient(), Objects: mgr.GetAPIReader(), Leases: mgr.GetClient(), Now: time.Now}
	if err := r.SetupWithManager(mgr); err != nil {
		t.Fatal(err)
	}
	startCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	started := make(chan error, 1)
	go func() { started <- mgr.Start(startCtx) }()
	if !mgr.GetCache().WaitForCacheSync(startCtx) {
		cancel()
		if err := <-started; err != nil {
			t.Fatalf("manager failed before its watch cache synced: %v", err)
		}
		t.Fatal("manager watch cache did not sync with the granted resource permissions")
	}
	cancel()
	if err := <-started; err != nil {
		t.Fatal(err)
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
		clientgoscheme.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme, kubevirtv1.AddToScheme, workloadapi.AddToScheme,
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
		Now: time.Now,
	}
	r.Credentials, err = requester.New(mgr.GetAPIReader(), mgr.GetClient())
	if err != nil {
		t.Fatal(err)
	}
	if err := r.SetupWithManager(mgr); err != nil {
		t.Fatal(err)
	}
	// The Policy Controller's issuer, which the Sandbox Controller requests its credentials from.
	if err := (&issuer.Reconciler{Client: mgr.GetClient(), APIReader: mgr.GetAPIReader(), Issuer: "test-runner-issuer"}).SetupWithManager(mgr); err != nil {
		t.Fatal(err)
	}
	if err := objects.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatal(err)
	}
	template := &sandboxapi.SandboxTemplate{ObjectMeta: metav1.ObjectMeta{Name: "environment-runtime", Namespace: ns}, Spec: sandboxapi.SandboxTemplateSpec{
		Pod: &corev1.PodTemplateSpec{Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: EnvironmentRuntimeContainer, Image: "example.invalid/runner:v1"}}}},
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
	agent := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Name: "integration-agent", Namespace: ns}, Spec: policyapi.IdentitySpec{Description: "Integration agent"}}
	if err := objects.Create(ctx, agent); err != nil {
		t.Fatal(err)
	}
	acting := &sandboxapi.IdentityRef{Name: agent.Name}
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: ns}, Spec: sandboxapi.SandboxSpec{
		Template: template.Name, Running: true, Identity: acting, Bootstrap: "echo desired",
	}}
	if err := objects.Create(ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	eventually(t, func() bool { return objects.Get(ctx, key, &corev1.Pod{}) == nil })
	// The app and Runner Gateway hold patch on Sandboxes but not sandboxes/status: neither the main
	// resource's status nor credential annotations on the Sandbox can rebind the runner credential.
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
	// The apiserver keeps both trust sources: the cluster bundle and the workload CA alone.
	tlsVolume := slices.IndexFunc(pod.Spec.Volumes, func(volume corev1.Volume) bool { return volume.Name == EnvironmentRuntimeTLSVolumeName })
	if tlsVolume < 0 || pod.Spec.Volumes[tlsVolume].Projected == nil || !slices.ContainsFunc(pod.Spec.Volumes[tlsVolume].Projected.Sources, func(source corev1.VolumeProjection) bool {
		return source.ConfigMap != nil && source.ConfigMap.Name == RunnerWorkloadTrustBundleName && slices.Equal(source.ConfigMap.Items, []corev1.KeyToPath{{Key: RunnerTrustBundleKey, Path: RunnerWorkloadTrustFile}})
	}) {
		t.Fatalf("runner Pod volumes = %#v, want the workload CA projected as %s", pod.Spec.Volumes, RunnerWorkloadTrustFile)
	}
	const otherID = "fedcba9876543210fedcba9876543210"
	if err := objects.Patch(ctx, sandbox, client.RawPatch(types.MergePatchType, fmt.Appendf(nil, `{
		"metadata": {"annotations": {%q: %q}},
		"status": {"runnerCredential": {"kind": "VM", "podUid": %q}}
	}`, sandboxapi.EnvironmentRuntimeCredentialMetadataKey, otherID, otherID))); err != nil {
		t.Fatal(err)
	}
	// A reconcile that read the patched Sandbox, by the next spec write.
	if err := objects.Get(ctx, key, sandbox); err != nil {
		t.Fatal(err)
	}
	sandbox.Annotations = map[string]string{"retry": "after-status-patch"}
	if err := objects.Update(ctx, sandbox); err != nil {
		t.Fatal(err)
	}
	var unchanged corev1.Pod
	if err := objects.Get(ctx, key, &unchanged); err != nil || unchanged.UID != pod.UID {
		t.Fatalf("spec writer's patch replaced the bound Pod: %v", err)
	}
	if err := objects.Get(ctx, key, sandbox); err != nil {
		t.Fatal(err)
	}
	if sandbox.Status.RunnerCredential == nil || *sandbox.Status.RunnerCredential != bound {
		t.Fatalf("spec writer's patch changed the binding to %#v, want %#v", sandbox.Status.RunnerCredential, bound)
	}
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
	for _, test := range []struct {
		name, reason string
		foreign      client.Object
	}{
		{"foreign-launch", "ConfigMapConflict", &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "foreign-launch-launch", Namespace: ns}}},
		{"foreign-service", "ServiceConflict", &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "foreign-service-control", Namespace: ns}, Spec: corev1.ServiceSpec{ClusterIP: corev1.ClusterIPNone}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := objects.Create(ctx, test.foreign); err != nil {
				t.Fatal(err)
			}
			other := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: test.name, Namespace: ns}, Spec: sandboxapi.SandboxSpec{Template: template.Name, Running: true, Identity: acting}}
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
		other := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Name: "template-watch", Namespace: ns}, Spec: sandboxapi.SandboxSpec{Template: changed.Name, Running: true, Identity: acting}}
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
		changed.Spec.Pod.Spec.Containers[0].Name = EnvironmentRuntimeContainer
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
		Spec:       sandboxapi.SandboxSpec{Template: vmTemplate.Name, Running: true, Identity: acting},
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
