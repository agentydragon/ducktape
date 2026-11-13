package sandboxcontroller

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/bazelbuild/rules_go/go/runfiles"
	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var vmGuestContractRlocation string

const workspaceVolume = "workspace"

type vmGuestContract struct {
	Disks struct {
		Launch struct {
			VolumeName string `json:"volume_name"`
			Serial     string `json:"serial"`
			Mount      string `json:"mount"`
		} `json:"launch"`
	} `json:"disks"`
	State struct {
		VolumeName string `json:"volume_name"`
	} `json:"state"`
	Files struct {
		Launch string `json:"launch"`
	} `json:"files"`
	EnvironmentRuntimeTLS struct {
		VolumeName  string `json:"volume_name"`
		RuntimePath string `json:"runtime_path"`
	} `json:"environment_runtime_tls"`
	EnvironmentRuntimeTrust struct {
		VolumeName string `json:"volume_name"`
		Mount      string `json:"mount"`
	} `json:"environment_runtime_trust"`
	EnvironmentRuntimeWorkloadTrust struct {
		VolumeName string `json:"volume_name"`
	} `json:"environment_runtime_workload_trust"`
}

func readVMGuestContract(t *testing.T) vmGuestContract {
	t.Helper()
	path, err := runfiles.Rlocation(vmGuestContractRlocation)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var contract vmGuestContract
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatal(err)
	}
	return contract
}

func testVMTemplate(image string) *sandboxapi.SandboxTemplate {
	template := testTemplate(image)
	template.Spec.Pod = nil
	template.Spec.VM = &kubevirtv1.VirtualMachineSpec{
		DataVolumeTemplates: []kubevirtv1.DataVolumeTemplateSpec{{ObjectMeta: metav1.ObjectMeta{Name: workspaceVolume}}},
		Template: &kubevirtv1.VirtualMachineInstanceTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app.kubernetes.io/name": "test-environment-vm"}},
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

// testWorkspaceVMTemplate is testVMTemplate with the workspace disk a PVC the controller creates
// instead of a DataVolume the VM does.
func testWorkspaceVMTemplate(image string) *sandboxapi.SandboxTemplate {
	template := testVMTemplate(image)
	template.Spec.VM.DataVolumeTemplates = nil
	volumes := &template.Spec.VM.Template.Spec.Volumes
	*volumes = slices.DeleteFunc(*volumes, func(v kubevirtv1.Volume) bool { return v.Name == workspaceVolume })
	for i := range template.Spec.VM.Template.Spec.Domain.Devices.Disks {
		disk := &template.Spec.VM.Template.Spec.Domain.Devices.Disks[i]
		if disk.Name == workspaceVolume {
			disk.Disk = &kubevirtv1.DiskTarget{Bus: "virtio"}
		}
	}
	template.Spec.WorkspaceVolume = &sandboxapi.WorkspaceVolume{Name: workspaceVolume, Layout: sandboxapi.VolumeLayout{Type: sandboxapi.RawDiskImage, Filesystem: "ext4"}, Claim: corev1.PersistentVolumeClaimSpec{
		AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
		Resources: corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{
			corev1.ResourceStorage: resource.MustParse("2Gi"),
		}},
	}}
	return template
}

func getWorkspacePVC(t *testing.T, c client.Client) *corev1.PersistentVolumeClaim {
	t.Helper()
	var pvc corev1.PersistentVolumeClaim
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: WorkspaceVolumeName(sandboxName)}, &pvc); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		t.Fatal(err)
	}
	return &pvc
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

func replaceTemplate(t *testing.T, c client.Client, template *sandboxapi.SandboxTemplate) {
	t.Helper()
	var current sandboxapi.SandboxTemplate
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
	contract := readVMGuestContract(t)
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
	if labels := vm.Spec.Template.ObjectMeta.Labels; labels[sandboxapi.SandboxLabel] != sandboxName || labels["app.kubernetes.io/name"] != "test-environment-vm" {
		t.Errorf("VMI template labels = %v; the VMI and its virt-launcher Pod need the Sandbox label", labels)
	}
	state := volume(vm, contract.State.VolumeName)
	if state == nil || state.PersistentVolumeClaim == nil || state.PersistentVolumeClaim.ClaimName != StateVolumeName(sandboxName) {
		t.Fatalf("VM state volume = %v, want the state PVC", state)
	}
	filesystems := vm.Spec.Template.Spec.Domain.Devices.Filesystems
	filesystemNames := make(map[string]bool, len(filesystems))
	for _, filesystem := range filesystems {
		filesystemNames[filesystem.Name] = filesystem.Virtiofs != nil
	}
	for _, tag := range []string{
		contract.State.VolumeName,
		contract.EnvironmentRuntimeTLS.VolumeName,
		contract.EnvironmentRuntimeTrust.VolumeName,
		contract.EnvironmentRuntimeWorkloadTrust.VolumeName,
	} {
		if got := len([]byte(tag)); got > maxVirtiofsTagBytes {
			t.Errorf("contract virtiofs tag %q is %d bytes, QEMU allows at most %d", tag, got, maxVirtiofsTagBytes)
		}
	}
	for _, filesystem := range filesystems {
		if got := len([]byte(filesystem.Name)); got > maxVirtiofsTagBytes {
			t.Errorf("emitted virtiofs tag %q is %d bytes, QEMU allows at most %d", filesystem.Name, got, maxVirtiofsTagBytes)
		}
	}
	if len(filesystems) != 4 || !filesystemNames[contract.State.VolumeName] || !filesystemNames[contract.EnvironmentRuntimeTLS.VolumeName] || !filesystemNames[contract.EnvironmentRuntimeTrust.VolumeName] || !filesystemNames[contract.EnvironmentRuntimeWorkloadTrust.VolumeName] {
		t.Errorf("VM filesystems = %v, want state and runner TLS/trust/workload trust volumes over virtiofs", filesystems)
	}
	if trust := volume(vm, contract.EnvironmentRuntimeTrust.VolumeName); trust == nil || trust.ConfigMap == nil || trust.ConfigMap.Name != RunnerTrustBundleName {
		t.Errorf("VM trust volume = %v, want the cluster bundle", trust)
	}
	if trust := volume(vm, contract.EnvironmentRuntimeWorkloadTrust.VolumeName); trust == nil || trust.ConfigMap == nil || trust.ConfigMap.Name != RunnerWorkloadTrustBundleName {
		t.Errorf("VM workload trust volume = %v, want the workload CA bundle", trust)
	}
	if LaunchDiskSerial != contract.Disks.Launch.Serial || LaunchMountPath != contract.Disks.Launch.Mount || LaunchFile != contract.Files.Launch {
		t.Fatalf("launch disk contract = serial %q, mount %q, file %q; want %q, %q, %q", LaunchDiskSerial, LaunchMountPath, LaunchFile, contract.Disks.Launch.Serial, contract.Disks.Launch.Mount, contract.Files.Launch)
	}
	launchDisk := slices.IndexFunc(vm.Spec.Template.Spec.Domain.Devices.Disks, func(disk kubevirtv1.Disk) bool {
		return disk.Serial == contract.Disks.Launch.Serial && disk.Name == contract.Disks.Launch.VolumeName
	})
	if launchDisk < 0 {
		t.Fatalf("VM disks = %v, want launch disk %s with serial %s", vm.Spec.Template.Spec.Domain.Devices.Disks, contract.Disks.Launch.VolumeName, contract.Disks.Launch.Serial)
	}
	launchVolume := volume(vm, contract.Disks.Launch.VolumeName)
	if launchVolume == nil || launchVolume.ConfigMap == nil || launchVolume.ConfigMap.Name != LaunchConfigMapName(sandboxName) {
		t.Fatalf("VM launch volume = %v, want the Sandbox launch ConfigMap", launchVolume)
	}
	if EnvironmentRuntimeTLSMountPath != contract.EnvironmentRuntimeTLS.RuntimePath || EnvironmentRuntimeTrustMountPath != contract.EnvironmentRuntimeTrust.Mount {
		t.Fatalf("Runtime credential paths = TLS %q, trust %q; want %q and %q", EnvironmentRuntimeTLSMountPath, EnvironmentRuntimeTrustMountPath, contract.EnvironmentRuntimeTLS.RuntimePath, contract.EnvironmentRuntimeTrust.Mount)
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

func TestRenderVMRejectsVirtiofsTagsOverQEMULimit(t *testing.T) {
	template := testVMTemplate("test-guest:v1")
	template.Spec.StateVolume.Name = strings.Repeat("s", maxVirtiofsTagBytes-1) + "é"
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: sandboxName}}

	vm, blocked := renderVM(sandbox, template, requester.Issued{ID: "0123456789abcdef0123456789abcdef", SecretName: runnerCredentialTestSecret})
	if vm != nil {
		t.Fatal("rendered a VM with an overlength virtiofs tag")
	}
	if blocked == nil || blocked.reason != "TemplateInvalid" || !strings.Contains(blocked.message, "37 UTF-8 bytes") {
		t.Fatalf("blocked = %v, want TemplateInvalid for a 37-byte QEMU virtiofs tag", blocked)
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
	if got := getCondition(t, c, sandboxapi.ConditionWorkloadReady); got.Status != metav1.ConditionTrue {
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
	if stage := getSandbox(t, c).Status.StopStage; stage != sandboxapi.StopStageStopped {
		t.Errorf("stopStage = %q, want Stopped", stage)
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
	template := testVMTemplate("test-guest:v1")
	// A VM without persistent guest disks can switch kinds without discarding their contents.
	template.Spec.VM.DataVolumeTemplates = nil
	template.Spec.VM.Template.Spec.Volumes = slices.DeleteFunc(template.Spec.VM.Template.Spec.Volumes, func(v kubevirtv1.Volume) bool { return v.DataVolume != nil })
	template.Spec.VM.Template.Spec.Domain.Devices.Disks = slices.DeleteFunc(template.Spec.VM.Template.Spec.Domain.Devices.Disks, func(d kubevirtv1.Disk) bool { return d.Name == workspaceVolume })
	r, c := newReconciler(t, true, template)
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
	var sandbox sandboxapi.Sandbox
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

func TestVMTemplateWithoutKubeVirtStaysUnreadyWithoutStartingWorkload(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("test-guest:v1"))
	r.KubeVirt = false
	reconcileOnce(t, r)
	if getVM(t, c) != nil || getPod(t, c) != nil {
		t.Fatal("created a workload for a VM template without KubeVirt")
	}
	if reason := runningReason(t, c); reason != "KubeVirtMissing" {
		t.Errorf("Running reason = %s, want KubeVirtMissing", reason)
	}
}

func TestWorkspaceVolumeIsAPVCOfTheSandboxAttachedToTheVM(t *testing.T) {
	template := testWorkspaceVMTemplate("test-guest:v1")
	r, c := newReconciler(t, true, template)
	reconcileOnce(t, r)

	pvc := getWorkspacePVC(t, c)
	if pvc == nil {
		t.Fatal("no workspace PVC")
	}
	if !metav1.IsControlledBy(pvc, getSandbox(t, c)) || pvc.Labels[sandboxapi.SandboxLabel] != sandboxName {
		t.Errorf("workspace PVC owners %v, labels %v; the Sandbox must own it and the cache select it by label", pvc.OwnerReferences, pvc.Labels)
	}
	if pvc.OwnerReferences[0].BlockOwnerDeletion == nil || *pvc.OwnerReferences[0].BlockOwnerDeletion {
		t.Error("the workspace PVC's owner reference blocks the Sandbox's deletion")
	}
	if !apiequality.Semantic.DeepEqual(pvc.Spec, template.Spec.WorkspaceVolume.Claim) {
		t.Errorf("workspace PVC spec = %v, want the template's claim", pvc.Spec)
	}
	vm := getVM(t, c)
	if v := volume(vm, workspaceVolume); v == nil || v.PersistentVolumeClaim == nil || v.PersistentVolumeClaim.ClaimName != pvc.Name {
		t.Errorf("VM workspace volume = %v, want the workspace PVC", v)
	}
	if len(vm.Spec.DataVolumeTemplates) != 0 {
		t.Errorf("VM DataVolume templates = %v, want none", vm.Spec.DataVolumeTemplates)
	}
}

func TestHaltedVMKeepsItsWorkspaceVolume(t *testing.T) {
	r, c := newReconciler(t, true, testWorkspaceVMTemplate("test-guest:v1"))
	reconcileOnce(t, r)
	pvc := getWorkspacePVC(t, c)
	pvc.Annotations["test.example/marker"] = "kept"
	if err := c.Update(context.Background(), pvc); err != nil {
		t.Fatal(err)
	}

	setRunning(t, c, false)
	reconcileOnce(t, r)
	setRunning(t, c, true)
	reconcileOnce(t, r)

	if pvc := getWorkspacePVC(t, c); pvc == nil || pvc.Annotations["test.example/marker"] != "kept" {
		t.Errorf("workspace PVC after stop and start = %v, want the one from before", pvc)
	}
	if v := volume(getVM(t, c), workspaceVolume); v == nil || v.PersistentVolumeClaim == nil {
		t.Errorf("restarted VM workspace volume = %v, want the PVC", v)
	}
}

func TestWorkspaceVolumeTheSandboxDoesNotOwnIsNotUsed(t *testing.T) {
	stale := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: WorkspaceVolumeName(sandboxName),
	}}
	r, c := newReconciler(t, true, testWorkspaceVMTemplate("test-guest:v1"), stale)
	reconcileOnce(t, r)
	if getVM(t, c) != nil {
		t.Fatal("started a VM on a previous Sandbox's workspace")
	}
	if reason := runningReason(t, c); reason != "VolumeLayoutMismatch" {
		t.Errorf("Running reason = %s, want VolumeLayoutMismatch", reason)
	}
}

func TestTemplateDefiningTheWorkspaceVolumeItselfIsRefused(t *testing.T) {
	template := testWorkspaceVMTemplate("test-guest:v1")
	template.Spec.VM.Template.Spec.Volumes = append(template.Spec.VM.Template.Spec.Volumes, kubevirtv1.Volume{
		Name:         workspaceVolume,
		VolumeSource: kubevirtv1.VolumeSource{EmptyDisk: &kubevirtv1.EmptyDiskSource{Capacity: resource.MustParse("1Gi")}},
	})
	r, c := newReconciler(t, true, template)
	reconcileOnce(t, r)
	if getVM(t, c) != nil || getWorkspacePVC(t, c) != nil {
		t.Fatal("started a VM, or made a workspace PVC, for a template that defines the workspace volume itself")
	}
	if reason := runningReason(t, c); reason != "VolumeLayoutMismatch" {
		t.Errorf("Running reason = %s, want VolumeLayoutMismatch", reason)
	}
}

func TestPodTemplateWithAWorkspaceVolumeIsRefused(t *testing.T) {
	template := testTemplate("test-image:v1")
	template.Spec.WorkspaceVolume = testWorkspaceVMTemplate("test-guest:v1").Spec.WorkspaceVolume
	r, c := newReconciler(t, true, template)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("started a Pod from a template that sets a workspace volume")
	}
	if reason := runningReason(t, c); reason != "VolumeLayoutMismatch" {
		t.Errorf("Running reason = %s, want VolumeLayoutMismatch", reason)
	}
}
