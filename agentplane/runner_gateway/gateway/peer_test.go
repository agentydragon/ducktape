package gateway

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	kubefake "k8s.io/client-go/kubernetes/fake"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestVMCertificateRequiresCurrentLauncherVMIAndSandboxOwnerChain(t *testing.T) {
	for name, change := range map[string]func(*sandboxapi.Sandbox, *corev1.Pod, *kubevirtv1.VirtualMachineInstance, *kubevirtv1.VirtualMachine){
		"current": func(*sandboxapi.Sandbox, *corev1.Pod, *kubevirtv1.VirtualMachineInstance, *kubevirtv1.VirtualMachine) {
		},
		"replaced launcher": func(s *sandboxapi.Sandbox, _ *corev1.Pod, _ *kubevirtv1.VirtualMachineInstance, _ *kubevirtv1.VirtualMachine) {
			s.Status.Incarnation.UID = "old-launcher-uid"
		},
		"replaced VMI": func(_ *sandboxapi.Sandbox, _ *corev1.Pod, vmi *kubevirtv1.VirtualMachineInstance, _ *kubevirtv1.VirtualMachine) {
			vmi.UID = "replacement-vmi-uid"
		},
		"replaced VM": func(_ *sandboxapi.Sandbox, _ *corev1.Pod, _ *kubevirtv1.VirtualMachineInstance, vm *kubevirtv1.VirtualMachine) {
			vm.UID = "replacement-vm-uid"
		},
		"foreign Sandbox": func(_ *sandboxapi.Sandbox, _ *corev1.Pod, _ *kubevirtv1.VirtualMachineInstance, vm *kubevirtv1.VirtualMachine) {
			vm.OwnerReferences[0].UID = "other-sandbox-uid"
		},
		"foreign VM group": func(_ *sandboxapi.Sandbox, _ *corev1.Pod, vmi *kubevirtv1.VirtualMachineInstance, _ *kubevirtv1.VirtualMachine) {
			vmi.OwnerReferences[0].APIVersion = "other.example/v1"
		},
		"stale credential": func(_ *sandboxapi.Sandbox, _ *corev1.Pod, vmi *kubevirtv1.VirtualMachineInstance, _ *kubevirtv1.VirtualMachine) {
			vmi.Labels[sandboxapi.RunnerCredentialLabel] = "new-credential"
		},
	} {
		t.Run(name, func(t *testing.T) {
			sandbox := &sandboxapi.Sandbox{
				ObjectMeta: metav1.ObjectMeta{Name: "runner", Namespace: "test-vm", UID: "sandbox-uid"},
				Status:     sandboxapi.SandboxStatus{Incarnation: &sandboxapi.Incarnation{Name: "launcher", UID: "launcher-uid"}},
			}
			vm := &kubevirtv1.VirtualMachine{ObjectMeta: metav1.ObjectMeta{
				Name: sandbox.Name, Namespace: sandbox.Namespace, UID: "vm-uid",
				OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(sandbox, sandboxapi.GroupVersion.WithKind(sandboxapi.SandboxKind))},
			}}
			vmi := &kubevirtv1.VirtualMachineInstance{ObjectMeta: metav1.ObjectMeta{
				Name: sandbox.Name, Namespace: sandbox.Namespace, UID: "vmi-uid",
				OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(vm, kubevirtv1.VirtualMachineGroupVersionKind)},
				Labels:          map[string]string{sandboxapi.SandboxLabel: sandbox.Name, sandboxapi.RunnerCredentialLabel: "current-credential"},
			}}
			pod := &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name: "launcher", Namespace: sandbox.Namespace, UID: "launcher-uid",
					OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(vmi, kubevirtv1.VirtualMachineInstanceGroupVersionKind)},
					Labels:          map[string]string{sandboxapi.SandboxLabel: sandbox.Name, sandboxapi.RunnerCredentialLabel: "untrusted-launcher-label"},
				},
				Spec:   corev1.PodSpec{ServiceAccountName: sandbox.Name},
				Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "192.0.2.90", Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}},
			}
			change(sandbox, pod, vmi, vm)
			scheme := runtime.NewScheme()
			for _, add := range []func(*runtime.Scheme) error{sandboxapi.AddToScheme, kubevirtv1.AddToScheme} {
				if err := add(scheme); err != nil {
					t.Fatal(err)
				}
			}
			objects := fake.NewClientBuilder().WithScheme(scheme).WithObjects(sandbox, vmi, vm).Build()
			g := &Gateway{
				config: Config{Namespace: sandbox.Namespace}, objects: objects,
				sandboxes: &sandboxResolver{core: kubefake.NewSimpleClientset(pod).CoreV1()},
			}
			uri, err := runner_transport.RunnerURI(sandbox.Namespace, string(sandbox.UID), "current-credential")
			if err != nil {
				t.Fatal(err)
			}
			identity, podUID, err := g.certificatePeer(t.Context(), uri)
			if name != "current" {
				if err == nil {
					t.Fatal("stale or foreign VM credential was accepted")
				}
				return
			}
			if err != nil || identity.UID != string(sandbox.UID) || podUID != string(pod.UID) {
				t.Fatalf("current VMI credential rejected: identity=%v, pod=%s, err=%v", identity, podUID, err)
			}
		})
	}
}
