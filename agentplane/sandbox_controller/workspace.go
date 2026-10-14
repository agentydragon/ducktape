package sandboxcontroller

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const WorkspaceRecordAnnotation = "agentplane.allegedly.works/workspace-record"

// The record is created atomically with the claim, so a restart before the status update can
// recover without interpreting contents from the current template.
// +kubebuilder:object:generate=false
type workspaceRecord struct {
	Layout VolumeLayout                     `json:"layout"`
	Claim  corev1.PersistentVolumeClaimSpec `json:"claim"`
}

func layoutMismatch(message string) *unready {
	return &unready{reason: "VolumeLayoutMismatch", message: message}
}

func claimMode(spec corev1.PersistentVolumeClaimSpec) corev1.PersistentVolumeMode {
	if spec.VolumeMode == nil {
		return corev1.PersistentVolumeFilesystem
	}
	return *spec.VolumeMode
}

func validateWorkspace(template *SandboxTemplate) *unready {
	w := template.Spec.WorkspaceVolume
	if w == nil {
		return nil
	}
	if w.Name == "" || w.Name == template.Spec.StateVolume.Name {
		return layoutMismatch("workspace volume name is empty or conflicts with the state volume")
	}
	switch w.Layout.Type {
	case DirectoryTree:
		if w.Layout.Filesystem != "" || claimMode(w.Claim) != corev1.PersistentVolumeFilesystem {
			return layoutMismatch("DirectoryTree requires a Filesystem claim and no filesystem field")
		}
	case RawDiskImage, BlockFilesystem:
		mode := corev1.PersistentVolumeFilesystem
		if w.Layout.Type == BlockFilesystem {
			mode = corev1.PersistentVolumeBlock
		}
		if w.Layout.Filesystem != "ext4" || claimMode(w.Claim) != mode {
			return layoutMismatch("disk layouts require explicit ext4 and a matching claim volumeMode")
		}
		if template.Spec.Pod != nil {
			return layoutMismatch("disk workspaces cannot attach to a Pod")
		}
	default:
		return layoutMismatch("workspace layout type is not supported")
	}
	if template.Spec.Pod != nil {
		if slices.ContainsFunc(template.Spec.Pod.Spec.Volumes, func(v corev1.Volume) bool { return v.Name == w.Name }) {
			return layoutMismatch("template defines the managed workspace volume itself")
		}
		mounted := false
		for _, container := range append(slices.Clone(template.Spec.Pod.Spec.Containers), template.Spec.Pod.Spec.InitContainers...) {
			if slices.ContainsFunc(container.VolumeDevices, func(v corev1.VolumeDevice) bool { return v.Name == w.Name }) {
				return layoutMismatch("DirectoryTree requires a regular Pod volume mount")
			}
			mounted = mounted || slices.ContainsFunc(container.VolumeMounts, func(v corev1.VolumeMount) bool { return v.Name == w.Name && v.MountPath != "" })
		}
		if !mounted {
			return layoutMismatch("Pod workspace has no volume mount of its declared name")
		}
		return nil
	}
	if template.Spec.VM == nil || template.Spec.VM.Template == nil {
		return layoutMismatch("VM template is missing its instance template")
	}
	instance := &template.Spec.VM.Template.Spec
	if slices.ContainsFunc(instance.Volumes, func(v kubevirtv1.Volume) bool { return v.Name == w.Name }) ||
		slices.ContainsFunc(template.Spec.VM.DataVolumeTemplates, func(v kubevirtv1.DataVolumeTemplateSpec) bool { return v.Name == w.Name }) ||
		slices.ContainsFunc(instance.Domain.Devices.Filesystems, func(v kubevirtv1.Filesystem) bool { return v.Name == w.Name }) {
		return layoutMismatch("template defines the managed workspace attachment itself")
	}
	disk := slices.ContainsFunc(instance.Domain.Devices.Disks, func(d kubevirtv1.Disk) bool { return d.Name == w.Name && d.Disk != nil })
	if w.Layout.Type == DirectoryTree {
		if slices.ContainsFunc(instance.Domain.Devices.Disks, func(d kubevirtv1.Disk) bool { return d.Name == w.Name }) {
			return layoutMismatch("DirectoryTree attaches to a VM through virtiofs, not a disk")
		}
	} else if !disk {
		return layoutMismatch("disk workspace needs a disk device of its declared name")
	}
	return nil
}

func (r *Reconciler) ensureWorkspaceVolume(ctx context.Context, sandbox *Sandbox, template *SandboxTemplate) (*unready, error) {
	workspace := template.Spec.WorkspaceVolume
	pvc, err := r.claim(ctx, sandbox, WorkspaceVolumeName(sandbox.Name))
	if err != nil {
		return nil, err
	}
	var binding *WorkspaceBinding
	if sandbox.Status.Volumes != nil {
		binding = sandbox.Status.Volumes.Workspace
	}
	if workspace == nil {
		// Legacy VM DataVolume claims have the same name but are not a managed workspace.
		if binding != nil || (pvc != nil && metav1.IsControlledBy(pvc, sandbox)) {
			return layoutMismatch("retained workspace cannot lose its layout declaration"), nil
		}
		return nil, nil
	}
	if blocked := validateWorkspace(template); blocked != nil {
		return blocked, nil
	}
	want := workspaceRecord{Layout: workspace.Layout, Claim: *workspace.Claim.DeepCopy()}
	mode := claimMode(want.Claim)
	want.Claim.VolumeMode = &mode
	if pvc == nil {
		if binding != nil {
			return layoutMismatch("the recorded workspace claim is missing"), nil
		}
		encoded, err := json.Marshal(want)
		if err != nil {
			return nil, err
		}
		pvc = &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
			Name: WorkspaceVolumeName(sandbox.Name), Namespace: sandbox.Namespace,
			Labels:      map[string]string{SandboxLabel: sandbox.Name},
			Annotations: map[string]string{WorkspaceRecordAnnotation: string(encoded)},
		}, Spec: *workspace.Claim.DeepCopy()}
		if err := controllerutil.SetControllerReference(sandbox, pvc, r.Scheme(), controllerutil.WithBlockOwnerDeletion(false)); err != nil {
			return nil, err
		}
		if err := r.Create(ctx, pvc); err != nil {
			// Re-read an AlreadyExists rather than admitting a potentially foreign concurrent claim.
			if !apierrors.IsAlreadyExists(err) {
				return nil, err
			}
			pvc, err = r.claim(ctx, sandbox, pvc.Name)
			if err != nil {
				return nil, err
			}
		}
	}
	if pvc == nil {
		return layoutMismatch("workspace claim disappeared during creation"), nil
	}
	if conflict := claimConflict(pvc, sandbox, "VolumeLayoutMismatch"); conflict != nil {
		return conflict, nil
	}
	var recorded workspaceRecord
	if err := json.Unmarshal([]byte(pvc.Annotations[WorkspaceRecordAnnotation]), &recorded); err != nil ||
		!equality.Semantic.DeepEqual(recorded, want) || claimMode(pvc.Spec) != claimMode(recorded.Claim) {
		return layoutMismatch(fmt.Sprintf("PVC %s has no matching creation-time workspace layout and claim record", pvc.Name)), nil
	}
	if pvc.UID == "" {
		return layoutMismatch("workspace claim has no UID"), nil
	}
	current := &WorkspaceBinding{Name: pvc.Name, UID: string(pvc.UID), Layout: recorded.Layout}
	if binding != nil && !equality.Semantic.DeepEqual(binding, current) {
		return layoutMismatch("workspace claim identity or layout differs from the recorded binding"), nil
	}
	if binding == nil {
		if sandbox.Status.Volumes == nil {
			sandbox.Status.Volumes = &SandboxVolumes{}
		}
		sandbox.Status.Volumes.Workspace = current
		// Persist the identity before a workload can start; a status conflict leaves the PVC intact.
		if err := r.Status().Update(ctx, sandbox); err != nil {
			return nil, err
		}
	}
	return nil, nil
}
