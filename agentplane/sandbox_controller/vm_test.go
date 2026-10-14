package sandboxcontroller

import (
	"context"
	"errors"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const workspaceVolume = "workspace"

func testVMTemplate(image string) *SandboxTemplate {
	template := testTemplate(image)
	template.Spec.Pod = nil
	template.Spec.VM = &kubevirtv1.VirtualMachineSpec{
		DataVolumeTemplates: []kubevirtv1.DataVolumeTemplateSpec{{ObjectMeta: metav1.ObjectMeta{Name: workspaceVolume}}},
		Template: &kubevirtv1.VirtualMachineInstanceTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app.kubernetes.io/name": "test-runner-vm"}},
			Spec: kubevirtv1.VirtualMachineInstanceSpec{
				Domain: kubevirtv1.DomainSpec{Devices: kubevirtv1.Devices{Disks: []kubevirtv1.Disk{{Name: "root"}, {Name: workspaceVolume}}}},
				Volumes: []kubevirtv1.Volume{
					{Name: "root", VolumeSource: kubevirtv1.VolumeSource{ContainerDisk: &kubevirtv1.ContainerDiskSource{Image: image}}},
					{Name: workspaceVolume, VolumeSource: kubevirtv1.VolumeSource{DataVolume: &kubevirtv1.DataVolumeSource{Name: workspaceVolume}}},
				},
			},
		},
	}
	return template
}

func getVM(t *testing.T, c client.Client) *kubevirtv1.VirtualMachine {
	t.Helper()
	var vm kubevirtv1.VirtualMachine
	if err := c.Get(context.Background(), sandboxKey, &vm); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		t.Fatal(err)
	}
	return &vm
}

func volume(vm *kubevirtv1.VirtualMachine, name string) *kubevirtv1.Volume {
	for i, v := range vm.Spec.Template.Spec.Volumes {
		if v.Name == name {
			return &vm.Spec.Template.Spec.Volumes[i]
		}
	}
	return nil
}

func replaceTemplate(t *testing.T, c client.Client, template *SandboxTemplate) {
	t.Helper()
	var current SandboxTemplate
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(template), &current); err != nil {
		t.Fatal(err)
	}
	current.Spec = template.Spec
	if err := c.Update(context.Background(), &current); err != nil {
		t.Fatal(err)
	}
}

// startVMI is KubeVirt running the VM: its VMI in phase, and the VMI's virt-launcher Pod.
func startVMI(t *testing.T, c client.Client, phase kubevirtv1.VirtualMachineInstancePhase) (*kubevirtv1.VirtualMachineInstance, *corev1.Pod) {
	t.Helper()
	ctx := context.Background()
	vm := getVM(t, c)
	vmi := &kubevirtv1.VirtualMachineInstance{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: sandboxName, UID: "uid-vmi",
		Labels:          vm.Spec.Template.ObjectMeta.Labels,
		OwnerReferences: []metav1.OwnerReference{controllerRef("VirtualMachine", vm.Name, vm.UID)},
	}}
	if err := c.Create(ctx, vmi); err != nil {
		t.Fatal(err)
	}
	vmi.Status.Phase = phase
	if err := c.Status().Update(ctx, vmi); err != nil {
		t.Fatal(err)
	}
	launcher := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: "virt-launcher-" + sandboxName + "-test", UID: "uid-launcher",
		Labels:          vmi.Labels,
		OwnerReferences: []metav1.OwnerReference{controllerRef("VirtualMachineInstance", vmi.Name, vmi.UID)},
	}}
	if err := c.Create(ctx, launcher); err != nil {
		t.Fatal(err)
	}
	return vmi, launcher
}

func controllerRef(kind, name string, uid types.UID) metav1.OwnerReference {
	controller := true
	return metav1.OwnerReference{APIVersion: kubevirtv1.SchemeGroupVersion.String(), Kind: kind, Name: name, UID: uid, Controller: &controller}
}

func TestVMTemplateGetsARunningVMWithStateOverVirtiofs(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)

	if getPod(t, c) != nil {
		t.Fatal("a VM template started a Pod")
	}
	vm := getVM(t, c)
	if vm == nil {
		t.Fatal("no VM")
	}
	if !metav1.IsControlledBy(vm, getSandbox(t, c)) {
		t.Errorf("VM owner references = %v", vm.OwnerReferences)
	}
	if !vmRunning(vm) {
		t.Errorf("VM run strategy = %v, want Always", vm.Spec.RunStrategy)
	}
	if labels := vm.Spec.Template.ObjectMeta.Labels; labels[SandboxLabel] != sandboxName || labels["app.kubernetes.io/name"] != "test-runner-vm" {
		t.Errorf("VMI template labels = %v; the VMI and its virt-launcher Pod need the Sandbox label", labels)
	}
	state := volume(vm, "state")
	if state == nil || state.PersistentVolumeClaim == nil || state.PersistentVolumeClaim.ClaimName != StateVolumeName(sandboxName) {
		t.Fatalf("VM state volume = %v, want the state PVC", state)
	}
	filesystems := vm.Spec.Template.Spec.Domain.Devices.Filesystems
	if len(filesystems) != 1 || filesystems[0].Name != "state" || filesystems[0].Virtiofs == nil {
		t.Errorf("VM filesystems = %v, want the state volume over virtiofs", filesystems)
	}
	// One template serves many Sandboxes, and DataVolume names are per namespace.
	workspace := sandboxName + "-" + workspaceVolume
	if dvs := vm.Spec.DataVolumeTemplates; len(dvs) != 1 || dvs[0].Name != workspace {
		t.Errorf("VM DataVolume templates = %v, want %s", dvs, workspace)
	}
	if v := volume(vm, workspaceVolume); v == nil || v.DataVolume == nil || v.DataVolume.Name != workspace {
		t.Errorf("VM workspace volume = %v, want DataVolume %s", v, workspace)
	}
	if reason := runningReason(t, c); reason != "Starting" {
		t.Errorf("Running reason = %s, want Starting", reason)
	}
}

func TestRunningVMReportsItsLauncherPodAsTheIncarnation(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)
	_, launcher := startVMI(t, c, kubevirtv1.Running)
	reconcileOnce(t, r)
	if reason := runningReason(t, c); reason != "VMRunning" {
		t.Errorf("Running reason = %s, want VMRunning", reason)
	}
	if incarnation := getSandbox(t, c).Status.Incarnation; incarnation == nil || incarnation.Name != launcher.Name || incarnation.UID != string(launcher.UID) {
		t.Errorf("status incarnation = %v, want the virt-launcher Pod", incarnation)
	}
}

func TestVMSandboxIsReadyWhenItsLauncherPodIsReady(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)
	_, launcher := startVMI(t, c, kubevirtv1.Running)
	reconcileOnce(t, r)
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionFalse || got.Reason != "PodNotReady" {
		t.Errorf("Ready = %s/%s with a launcher that is not Ready, want False/PodNotReady", got.Status, got.Reason)
	}
	setPodReady(t, c, launcher, corev1.ConditionTrue)
	reconcileOnce(t, r)
	if got := getCondition(t, c, WorkloadReady); got.Status != metav1.ConditionTrue {
		t.Errorf("WorkloadReady = %s/%s with a Ready launcher, want True", got.Status, got.Reason)
	}
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionFalse || got.Reason != "NeverConnected" {
		t.Errorf("Ready = %s/%s with a guest runner that never reached the gateway, want False/NeverConnected", got.Status, got.Reason)
	}
	connect(t, c, string(launcher.UID))
	reconcileOnce(t, r)
	if got := getReadyCondition(t, c); got.Status != metav1.ConditionTrue {
		t.Errorf("Ready = %s/%s with a connected guest runner, want True", got.Status, got.Reason)
	}
}

func TestStopHaltsTheVMAndKeepsIt(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)
	vmi, _ := startVMI(t, c, kubevirtv1.Running)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	vm := getVM(t, c)
	if vm == nil || vmRunning(vm) {
		t.Fatalf("stopped VM = %v, want it kept and Halted", vm)
	}
	if reason := runningReason(t, c); reason != "Stopping" {
		t.Errorf("Running reason = %s, want Stopping while the VMI is there", reason)
	}
	if err := c.Delete(context.Background(), vmi); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if reason := runningReason(t, c); reason != "Stopped" {
		t.Errorf("Running reason = %s, want Stopped", reason)
	}
	if getPVC(t, c) == nil {
		t.Fatal("stopping deleted the state PVC")
	}
}

func TestStartRendersAHaltedVMFromTheTemplateAsItIsThen(t *testing.T) {
	r, c := newReconciler(t, false, testVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)
	setRunning(t, c, true)
	reconcileOnce(t, r)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	before := getVM(t, c)

	replaceTemplate(t, c, testVMTemplate("test-guest:v2"))
	setRunning(t, c, true)
	reconcileOnce(t, r)
	vm := getVM(t, c)
	if vm.UID != before.UID {
		t.Fatal("start replaced the halted VM, and with it its disks")
	}
	if !vmRunning(vm) || volume(vm, "root").ContainerDisk.Image != "test-guest:v2" {
		t.Errorf("started VM runs %v with image %s, want Always with the template's new image", vm.Spec.RunStrategy, volume(vm, "root").ContainerDisk.Image)
	}
}

func TestSwitchToAPodTemplateDeletesTheVMBeforeThePodStarts(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)
	setRunning(t, c, false)
	reconcileOnce(t, r)

	replaceTemplate(t, c, testTemplate("test-image:v1"))
	setRunning(t, c, true)
	reconcileOnce(t, r)
	if getVM(t, c) != nil || getPod(t, c) != nil {
		t.Fatal("the Pod was started before the VM went")
	}
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("no Pod once the VM was gone")
	}
	if getPVC(t, c) == nil {
		t.Fatal("switching kinds deleted the state PVC")
	}
}

func TestVMDeletionWaitsForTheVMIBeforeTheStateVolume(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)
	vmi, _ := startVMI(t, c, kubevirtv1.Running)
	connect(t, c, "uid-runner")
	r.Quiescer.(*fakeQuiescer).err = errors.New("runner unreachable")
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	if getVM(t, c) == nil {
		t.Fatal("deletion removed the VM before the Sessions were sealed")
	}
	quiesceSessions(t, r)
	reconcileOnce(t, r)
	if getVM(t, c) != nil {
		t.Fatal("deletion left the VM once the Sessions were sealed")
	}
	reconcileOnce(t, r)
	if getPVC(t, c) == nil {
		t.Fatal("the state PVC went while the VMI's guest could still be writing to it")
	}
	if err := c.Delete(context.Background(), vmi); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	var sandbox Sandbox
	if err := c.Get(context.Background(), sandboxKey, &sandbox); !apierrors.IsNotFound(err) {
		t.Fatalf("Sandbox still present once the VMI was gone: %v", err)
	}
}

func TestTemplateWithBothKindsIsRefused(t *testing.T) {
	template := testVMTemplate("test-guest:v1")
	template.Spec.Pod = testTemplate("test-image:v1").Spec.Pod
	r, c := newReconciler(t, true, template)
	reconcileOnce(t, r)
	if getVM(t, c) != nil || getPod(t, c) != nil || getPVC(t, c) != nil {
		t.Fatal("created resources from a template of both kinds")
	}
	if reason := runningReason(t, c); reason != "TemplateInvalid" {
		t.Errorf("Running reason = %s, want TemplateInvalid", reason)
	}
}

func TestVMTemplateWithoutKubeVirtStaysUnreadyAndCreatesNothing(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	r.KubeVirt = false
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("created a Pod for a VM template")
	}
	if reason := runningReason(t, c); reason != "KubeVirtMissing" {
		t.Errorf("Running reason = %s, want KubeVirtMissing", reason)
	}
}
