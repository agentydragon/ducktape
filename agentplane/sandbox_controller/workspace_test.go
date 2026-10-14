package sandboxcontroller

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
)

func workspaceBinding(sandbox *Sandbox) *WorkspaceBinding {
	if sandbox.Status.Volumes == nil {
		return nil
	}
	return sandbox.Status.Volumes.Workspace
}

func workspaceTemplate(kind string, layout VolumeLayoutType) *SandboxTemplate {
	template := testWorkspaceVMTemplate("guest:v1")
	w := template.Spec.WorkspaceVolume
	w.Layout = VolumeLayout{Type: layout}
	if layout != DirectoryTree {
		w.Layout.Filesystem = "ext4"
	}
	if layout == BlockFilesystem {
		mode := corev1.PersistentVolumeBlock
		w.Claim.VolumeMode = &mode
	}
	if kind == "pod" {
		pod := testTemplate("runner:v1")
		pod.Spec.WorkspaceVolume = w
		pod.Spec.Pod.Spec.Containers[0].VolumeMounts = append(pod.Spec.Pod.Spec.Containers[0].VolumeMounts,
			corev1.VolumeMount{Name: w.Name, MountPath: "/workspace"})
		return pod
	}
	if layout == DirectoryTree {
		template.Spec.VM.Template.Spec.Domain.Devices.Disks = slices.DeleteFunc(template.Spec.VM.Template.Spec.Domain.Devices.Disks,
			func(d kubevirtv1.Disk) bool { return d.Name == w.Name })
	}
	return template
}

func TestWorkspaceCreationRecordsLayoutAndRendersSupportedAttachments(t *testing.T) {
	for _, scenario := range []struct {
		kind   string
		layout VolumeLayoutType
	}{
		{"pod", DirectoryTree}, {"vm", DirectoryTree}, {"vm", RawDiskImage}, {"vm", BlockFilesystem},
	} {
		t.Run(scenario.kind+"/"+string(scenario.layout), func(t *testing.T) {
			template := workspaceTemplate(scenario.kind, scenario.layout)
			r, c := newReconciler(t, true, template)
			reconcileOnce(t, r)
			pvc := getWorkspacePVC(t, c)
			if pvc == nil {
				t.Fatal("no workspace claim")
			}
			var record workspaceRecord
			if err := json.Unmarshal([]byte(pvc.Annotations[WorkspaceRecordAnnotation]), &record); err != nil {
				t.Fatal(err)
			}
			if record.Layout != template.Spec.WorkspaceVolume.Layout {
				t.Fatalf("layout = %v", record.Layout)
			}
			binding := getSandbox(t, c).Status.Volumes.Workspace
			if binding.Name != pvc.Name || binding.UID != string(pvc.UID) || binding.Layout != record.Layout {
				t.Fatalf("binding = %v", binding)
			}
			if scenario.kind == "pod" {
				pod := getPod(t, c)
				if !slices.ContainsFunc(pod.Spec.Volumes, func(v corev1.Volume) bool {
					return v.Name == workspaceVolume && v.PersistentVolumeClaim != nil && v.PersistentVolumeClaim.ClaimName == pvc.Name
				}) {
					t.Fatal("Pod did not attach retained workspace")
				}
			} else {
				vm := getVM(t, c)
				if volume(vm, workspaceVolume).PersistentVolumeClaim.ClaimName != pvc.Name {
					t.Fatal("VM did not attach retained claim")
				}
				virtiofs := slices.ContainsFunc(vm.Spec.Template.Spec.Domain.Devices.Filesystems, func(f kubevirtv1.Filesystem) bool { return f.Name == workspaceVolume && f.Virtiofs != nil })
				if virtiofs != (scenario.layout == DirectoryTree) {
					t.Fatalf("virtiofs = %v", virtiofs)
				}
			}
		})
	}
}

func TestWorkspaceChangeRefusesResumeAndRetainsClaimAndHaltedVM(t *testing.T) {
	changes := map[string]func(*SandboxTemplate){
		"layout": func(s *SandboxTemplate) {
			s.Spec.WorkspaceVolume.Layout = VolumeLayout{Type: DirectoryTree}
			s.Spec.VM.Template.Spec.Domain.Devices.Disks = slices.DeleteFunc(s.Spec.VM.Template.Spec.Domain.Devices.Disks, func(d kubevirtv1.Disk) bool { return d.Name == workspaceVolume })
		},
		"filesystem": func(s *SandboxTemplate) { s.Spec.WorkspaceVolume.Layout.Filesystem = "xfs" },
		"mode": func(s *SandboxTemplate) {
			mode := corev1.PersistentVolumeBlock
			s.Spec.WorkspaceVolume.Claim.VolumeMode = &mode
			s.Spec.WorkspaceVolume.Layout.Type = BlockFilesystem
		},
		"claim": func(s *SandboxTemplate) {
			s.Spec.WorkspaceVolume.Claim.Resources.Requests[corev1.ResourceStorage] = resource.MustParse("3Gi")
		},
		"removed": func(s *SandboxTemplate) { s.Spec.WorkspaceVolume = nil },
		"pod":     func(s *SandboxTemplate) { *s = *workspaceTemplate("pod", DirectoryTree) },
	}
	for name, change := range changes {
		t.Run(name, func(t *testing.T) {
			template := workspaceTemplate("vm", RawDiskImage)
			r, c := newReconciler(t, true, template)
			reconcileOnce(t, r)
			pvc := getWorkspacePVC(t, c)
			pvc.Annotations["test.example/data-marker"] = "retain"
			if err := c.Update(context.Background(), pvc); err != nil {
				t.Fatal(err)
			}
			setRunning(t, c, false)
			reconcileOnce(t, r)
			before := getVM(t, c)
			change(template)
			replaceTemplate(t, c, template)
			setRunning(t, c, true)
			reconcileOnce(t, r)
			after := getWorkspacePVC(t, c)
			if after.UID != pvc.UID || after.Annotations["test.example/data-marker"] != "retain" {
				t.Fatal("workspace data/identity was replaced")
			}
			if !equality.Semantic.DeepEqual(getVM(t, c).Spec, before.Spec) || getPod(t, c) != nil {
				t.Fatal("mismatch changed workload before refusing")
			}
			if runningReason(t, c) != "VolumeLayoutMismatch" {
				t.Fatalf("reason = %s", runningReason(t, c))
			}
		})
	}
}

func TestWorkspaceRestartRecoversCreationRecordAfterFailedStatusWrite(t *testing.T) {
	template := workspaceTemplate("vm", RawDiskImage)
	r, c := newReconciler(t, true, template)
	failure := errors.New("controller interrupted before binding persistence")
	r.Client = interceptor.NewClient(c.(client.WithWatch), interceptor.Funcs{
		SubResourceUpdate: func(ctx context.Context, c client.Client, name string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			if sandbox, ok := obj.(*Sandbox); ok && workspaceBinding(sandbox) != nil {
				return failure
			}
			return c.SubResource(name).Update(ctx, obj, opts...)
		},
	})
	if _, err := r.Reconcile(context.Background(), request); !errors.Is(err, failure) {
		t.Fatalf("error = %v", err)
	}
	pvc := getWorkspacePVC(t, c)
	if pvc == nil || getVM(t, c) != nil || workspaceBinding(getSandbox(t, c)) != nil {
		t.Fatal("claim creation and binding/start ordering was not preserved")
	}
	restarted := *r
	restarted.Client = c
	reconcileOnce(t, &restarted)
	if got := getSandbox(t, c).Status.Volumes.Workspace; got.UID != string(pvc.UID) || got.Layout != template.Spec.WorkspaceVolume.Layout {
		t.Fatalf("recovered binding = %v", got)
	}
	if getVM(t, c) == nil {
		t.Fatal("recorded claim did not recover")
	}
}

func TestWorkspaceClaimCollisionsFailClosed(t *testing.T) {
	for _, change := range []string{"unrecorded", "foreign", "deleting", "replaced-uid", "record-conflict", "actual-mode"} {
		t.Run(change, func(t *testing.T) {
			r, c := newReconciler(t, true, workspaceTemplate("vm", RawDiskImage))
			reconcileOnce(t, r)
			setRunning(t, c, false)
			reconcileOnce(t, r)
			before := getVM(t, c)
			pvc := getWorkspacePVC(t, c)
			switch change {
			case "unrecorded":
				delete(pvc.Annotations, WorkspaceRecordAnnotation)
			case "foreign":
				pvc.OwnerReferences[0].UID = "another-sandbox"
			case "deleting":
				pvc.Finalizers = []string{"test.example/hold"}
			case "replaced-uid":
				if err := c.Delete(context.Background(), pvc); err != nil {
					t.Fatal(err)
				}
				pvc.ResourceVersion = ""
				pvc.UID = types.UID("replacement")
				if err := c.Create(context.Background(), pvc); err != nil {
					t.Fatal(err)
				}
			case "record-conflict":
				pvc.Annotations[WorkspaceRecordAnnotation] = `{"layout":{"type":"DirectoryTree"}}`
			case "actual-mode":
				mode := corev1.PersistentVolumeBlock
				pvc.Spec.VolumeMode = &mode
			}
			if change != "replaced-uid" {
				if err := c.Update(context.Background(), pvc); err != nil {
					t.Fatal(err)
				}
			}
			if change == "deleting" {
				if err := c.Delete(context.Background(), pvc); err != nil {
					t.Fatal(err)
				}
			}
			setRunning(t, c, true)
			reconcileOnce(t, r)
			if runningReason(t, c) != "VolumeLayoutMismatch" {
				t.Fatalf("reason = %s", runningReason(t, c))
			}
			if !equality.Semantic.DeepEqual(getVM(t, c).Spec, before.Spec) {
				t.Fatal("collision changed halted VM")
			}
		})
	}
}

func TestInvalidWorkspaceDeclarationStartsNothing(t *testing.T) {
	for _, change := range []string{"missing-layout", "missing-filesystem", "unsupported-filesystem", "mode", "pod-disk", "directory-filesystem", "pod-no-mount"} {
		t.Run(change, func(t *testing.T) {
			template := workspaceTemplate("vm", RawDiskImage)
			switch change {
			case "missing-layout":
				template.Spec.WorkspaceVolume.Layout = VolumeLayout{}
			case "missing-filesystem":
				template.Spec.WorkspaceVolume.Layout.Filesystem = ""
			case "unsupported-filesystem":
				template.Spec.WorkspaceVolume.Layout.Filesystem = "xfs"
			case "mode":
				mode := corev1.PersistentVolumeBlock
				template.Spec.WorkspaceVolume.Claim.VolumeMode = &mode
			case "pod-disk":
				template = workspaceTemplate("pod", RawDiskImage)
			case "directory-filesystem":
				template = workspaceTemplate("pod", DirectoryTree)
				template.Spec.WorkspaceVolume.Layout.Filesystem = "ext4"
			case "pod-no-mount":
				template = workspaceTemplate("pod", DirectoryTree)
				template.Spec.Pod.Spec.Containers[0].VolumeMounts = nil
			}
			r, c := newReconciler(t, true, template)
			reconcileOnce(t, r)
			if getWorkspacePVC(t, c) != nil || getVM(t, c) != nil || getPod(t, c) != nil {
				t.Fatal("invalid declaration created workspace/workload")
			}
			if runningReason(t, c) != "VolumeLayoutMismatch" {
				t.Fatalf("reason = %s", runningReason(t, c))
			}
		})
	}
}

func TestCustomVMDataVolumeWorkspaceResumesWithoutManagedDeclaration(t *testing.T) {
	legacy := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: WorkspaceVolumeName(sandboxName), UID: "legacy-dv-pvc"}}
	r, c := newReconciler(t, true, testVMTemplate("guest:v1"), legacy)
	reconcileOnce(t, r)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	setRunning(t, c, true)
	reconcileOnce(t, r)
	if !vmRunning(getVM(t, c)) || getWorkspacePVC(t, c).UID != legacy.UID || workspaceBinding(getSandbox(t, c)) != nil {
		t.Fatal("custom VM DataVolume workspace was adopted or blocked")
	}
}

func TestOwnedUnrecordedWorkspaceIsNeverAdoptedOnFirstStart(t *testing.T) {
	controller := true
	claim := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: WorkspaceVolumeName(sandboxName), UID: "unrecorded-claim",
		OwnerReferences: []metav1.OwnerReference{{APIVersion: GroupVersion.String(), Kind: "Sandbox", Name: sandboxName, UID: "uid-sandbox", Controller: &controller}},
	}}
	r, c := newReconciler(t, true, workspaceTemplate("vm", RawDiskImage), claim)
	reconcileOnce(t, r)
	if getVM(t, c) != nil || workspaceBinding(getSandbox(t, c)) != nil || getWorkspacePVC(t, c).UID != claim.UID {
		t.Fatal("unrecorded owned claim was adopted or replaced")
	}
	if runningReason(t, c) != "VolumeLayoutMismatch" {
		t.Fatalf("reason = %s", runningReason(t, c))
	}
}

func TestLaunchGateKeepsEndedWorkloadBeforeReplacement(t *testing.T) {
	template := workspaceTemplate("pod", DirectoryTree)
	r, c := newReconciler(t, true, template)
	reconcileOnce(t, r)
	pod := getPod(t, c)
	pod.Status.Phase = corev1.PodFailed
	if err := c.Status().Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	var account corev1.ServiceAccount
	if err := c.Get(context.Background(), sandboxKey, &account); err != nil {
		t.Fatal(err)
	}
	account.OwnerReferences[0].UID = "another-owner"
	if err := c.Update(context.Background(), &account); err != nil {
		t.Fatal(err)
	}
	template.Spec.WorkspaceVolume.Layout = VolumeLayout{Type: RawDiskImage, Filesystem: "ext4"}
	replaceTemplate(t, c, template)
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("launch gate bypassed the retained-volume validation before deleting the ended Pod")
	}
}

func TestDirectoryWorkspaceSurvivesCompatibleVMToPodTransition(t *testing.T) {
	r, c := newReconciler(t, true, workspaceTemplate("vm", DirectoryTree))
	reconcileOnce(t, r)
	claim := getWorkspacePVC(t, c)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	replaceTemplate(t, c, workspaceTemplate("pod", DirectoryTree))
	setRunning(t, c, true)
	reconcileOnce(t, r)
	reconcileOnce(t, r)
	if getPod(t, c) == nil || getVM(t, c) != nil || getWorkspacePVC(t, c).UID != claim.UID {
		t.Fatal("compatible attachment transition lost the retained claim")
	}
}

func TestKindChangeCannotDiscardCustomVMDataVolumeDisks(t *testing.T) {
	r, c := newReconciler(t, true, testVMTemplate("guest:v1"))
	reconcileOnce(t, r)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	before := getVM(t, c)
	replaceTemplate(t, c, testTemplate("runner:v1"))
	setRunning(t, c, true)
	reconcileOnce(t, r)
	if getPod(t, c) != nil || !equality.Semantic.DeepEqual(before.Spec, getVM(t, c).Spec) {
		t.Fatal("kind change discarded a VM's retained disks")
	}
	if runningReason(t, c) != "VolumeLayoutMismatch" {
		t.Fatalf("reason = %s", runningReason(t, c))
	}
}

func TestSealedSuspendedSandboxCanDeleteDespiteChangedWorkspaceLayout(t *testing.T) {
	template := workspaceTemplate("vm", RawDiskImage)
	r, c := newReconciler(t, true, template)
	reconcileOnce(t, r)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	sandbox := getSandbox(t, c)
	meta.SetStatusCondition(&sandbox.Status.Conditions, metav1.Condition{Type: Quiesced, Status: metav1.ConditionTrue, Reason: "Quiesced", Message: "sealed before suspend", LastTransitionTime: metav1.NewTime(testNow)})
	if err := c.Status().Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
	template.Spec.WorkspaceVolume.Layout.Filesystem = "xfs"
	replaceTemplate(t, c, template)
	deleteSandbox(t, c)
	reconcileOnce(t, r)
	reconcileOnce(t, r)
	var remaining Sandbox
	if err := c.Get(context.Background(), sandboxKey, &remaining); !apierrors.IsNotFound(err) {
		t.Fatalf("sealed Sandbox deletion blocked: %v", err)
	}
}

func TestTemplateEditAfterBindingPersistenceCannotChangeValidatedAttachment(t *testing.T) {
	r, c := newReconciler(t, true, workspaceTemplate("vm", RawDiskImage))
	changed := false
	r.Client = interceptor.NewClient(c.(client.WithWatch), interceptor.Funcs{
		SubResourceUpdate: func(ctx context.Context, c client.Client, name string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			if err := c.SubResource(name).Update(ctx, obj, opts...); err != nil {
				return err
			}
			if sandbox, ok := obj.(*Sandbox); ok && workspaceBinding(sandbox) != nil && !changed {
				changed = true
				replaceTemplate(t, c, workspaceTemplate("vm", DirectoryTree))
			}
			return nil
		},
	})
	reconcileOnce(t, r)
	if !changed {
		t.Fatal("template did not change at the persistence boundary")
	}
	vm := getVM(t, c)
	if vm == nil {
		t.Fatal("validated workload did not start")
	}
	if slices.ContainsFunc(vm.Spec.Template.Spec.Domain.Devices.Filesystems, func(f kubevirtv1.Filesystem) bool { return f.Name == workspaceVolume }) {
		t.Fatal("a template reread attached the recorded raw disk image as a directory tree")
	}
	if !slices.ContainsFunc(vm.Spec.Template.Spec.Domain.Devices.Disks, func(d kubevirtv1.Disk) bool { return d.Name == workspaceVolume && d.Disk != nil }) {
		t.Fatal("validated disk attachment was lost")
	}
	if got := getSandbox(t, c).Status.Volumes.Workspace.Layout.Type; got != RawDiskImage {
		t.Fatalf("recorded layout = %s", got)
	}
}
