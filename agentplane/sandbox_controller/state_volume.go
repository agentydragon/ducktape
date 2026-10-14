package sandboxcontroller

import (
	"context"
	"encoding/json"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const StateVolumeRecordAnnotation = "agentplane.allegedly.works/state-volume-record"

// The record is created with the PVC, so a restart before the status update recovers its layout.
// +kubebuilder:object:generate=false
type stateVolumeRecord struct {
	Layout VolumeLayout `json:"layout"`
}

func stateVolumeLayout(volume StateVolume) VolumeLayout {
	if volume.Layout != nil {
		return *volume.Layout
	}
	return VolumeLayout{Type: DirectoryTree}
}

func validateStateVolume(template *SandboxTemplate) *unready {
	state := template.Spec.StateVolume
	if state.Name == "" {
		return layoutMismatch("state volume name is empty")
	}
	layout := stateVolumeLayout(state)
	if layout.Type != DirectoryTree || layout.Filesystem != "" || claimMode(state.Claim) != corev1.PersistentVolumeFilesystem {
		return layoutMismatch("state volume currently supports DirectoryTree on a Filesystem claim")
	}
	return nil
}

// ensureStateVolume records a state PVC's layout and identity before a workload can use it.
func (r *Reconciler) ensureStateVolume(ctx context.Context, sandbox *Sandbox) (*unready, error) {
	pvc, err := r.stateVolume(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	var binding *StateBinding
	if sandbox.Status.Volumes != nil {
		binding = sandbox.Status.Volumes.State
	}
	if pvc == nil {
		if binding != nil {
			return layoutMismatch("the recorded state claim is missing"), nil
		}
		template, blocked, err := r.template(ctx, sandbox)
		if err != nil || blocked != nil {
			return blocked, err
		}
		encoded, err := json.Marshal(stateVolumeRecord{Layout: stateVolumeLayout(template.Spec.StateVolume)})
		if err != nil {
			return nil, err
		}
		pvc = &corev1.PersistentVolumeClaim{
			ObjectMeta: metav1.ObjectMeta{
				Name:        StateVolumeName(sandbox.Name),
				Namespace:   sandbox.Namespace,
				Labels:      map[string]string{SandboxLabel: sandbox.Name},
				Annotations: map[string]string{StateVolumeRecordAnnotation: string(encoded)},
			},
			Spec: *template.Spec.StateVolume.Claim.DeepCopy(),
		}
		if err := controllerutil.SetControllerReference(sandbox, pvc, r.Scheme(), controllerutil.WithBlockOwnerDeletion(false)); err != nil {
			return nil, err
		}
		if err := r.Create(ctx, pvc); err != nil && !apierrors.IsAlreadyExists(err) {
			return nil, err
		}
		pvc, err = r.stateVolume(ctx, sandbox)
		if err != nil {
			return nil, err
		}
	}
	if pvc == nil {
		return layoutMismatch("state claim disappeared during creation"), nil
	}
	if conflict := claimConflict(pvc, sandbox, "StateVolumeConflict"); conflict != nil {
		return conflict, nil
	}
	if claimMode(pvc.Spec) != corev1.PersistentVolumeFilesystem {
		return layoutMismatch("the state PVC must remain a DirectoryTree on a Filesystem claim"), nil
	}
	if pvc.Annotations == nil {
		pvc.Annotations = map[string]string{}
	}
	recordText, hasRecord := pvc.Annotations[StateVolumeRecordAnnotation]
	if !hasRecord {
		if binding != nil {
			return layoutMismatch("the recorded state claim has lost its creation record"), nil
		}
		// Before this record existed, every controller-created state PVC was a DirectoryTree on a
		// Filesystem claim. Recover that fixed historical contract from the owned PVC, not its template.
		before := pvc.DeepCopy()
		encoded, err := json.Marshal(stateVolumeRecord{Layout: VolumeLayout{Type: DirectoryTree}})
		if err != nil {
			return nil, err
		}
		pvc.Annotations[StateVolumeRecordAnnotation] = string(encoded)
		if err := r.Patch(ctx, pvc, client.MergeFrom(before)); err != nil {
			return nil, err
		}
		recordText = pvc.Annotations[StateVolumeRecordAnnotation]
	}
	var recorded stateVolumeRecord
	if err := json.Unmarshal([]byte(recordText), &recorded); err != nil ||
		recorded.Layout.Type != DirectoryTree || recorded.Layout.Filesystem != "" {
		return layoutMismatch(fmt.Sprintf("PVC %s has no supported state layout record", pvc.Name)), nil
	}
	if pvc.UID == "" {
		return layoutMismatch("state claim has no UID"), nil
	}
	current := &StateBinding{Name: pvc.Name, UID: string(pvc.UID), Layout: recorded.Layout}
	if binding != nil && !equality.Semantic.DeepEqual(binding, current) {
		return layoutMismatch("state claim identity or layout differs from its recorded binding"), nil
	}
	if binding == nil {
		if sandbox.Status.Volumes == nil {
			sandbox.Status.Volumes = &SandboxVolumes{}
		}
		sandbox.Status.Volumes.State = current
		// Persist the identity before a workload can start; a status conflict leaves the PVC intact.
		if err := r.Status().Update(ctx, sandbox); err != nil {
			return nil, err
		}
	}
	return nil, nil
}
