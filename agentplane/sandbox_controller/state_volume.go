package sandboxcontroller

import (
	"context"
	"encoding/json"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const StateVolumeRecordAnnotation = "agentplane.allegedly.works/state-volume-record"

// The record is created with the PVC, so a restart before the status update recovers its layout.
// +kubebuilder:object:generate=false
type stateVolumeRecord struct {
	Layout VolumeLayout `json:"layout"`
}

func validateStateVolume(template *SandboxTemplate) *unready {
	state := template.Spec.StateVolume
	if state.Name == "" {
		return layoutMismatch("state volume name is empty")
	}
	layout := state.Layout
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
		encoded, err := json.Marshal(stateVolumeRecord{Layout: template.Spec.StateVolume.Layout})
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
	recordText, hasRecord := pvc.Annotations[StateVolumeRecordAnnotation]
	if !hasRecord {
		return layoutMismatch("the state claim has no creation record"), nil
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
