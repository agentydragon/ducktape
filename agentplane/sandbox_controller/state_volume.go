package sandboxcontroller

import (
	"context"
	"encoding/json"
	"fmt"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
)

const StateVolumeRecordAnnotation = "agentplane.allegedly.works/state-volume-record"

// The record is created with the PVC, so a restart before the status update recovers its layout.
// +kubebuilder:object:generate=false
type stateVolumeRecord struct {
	Layout sandboxapi.VolumeLayout `json:"layout"`
}

func validateStateVolume(template *sandboxapi.SandboxTemplate) *unready {
	state := template.Spec.StateVolume
	if state.Name == "" {
		return layoutMismatch("state volume name is empty")
	}
	layout := state.Layout
	if layout.Type != sandboxapi.DirectoryTree || layout.Filesystem != "" || claimMode(state.Claim) != corev1.PersistentVolumeFilesystem {
		return layoutMismatch("state volume currently supports DirectoryTree on a Filesystem claim")
	}
	return nil
}

// ensureStateVolume records a state PVC's layout and identity before a workload can use it.
func (r *Reconciler) ensureStateVolume(ctx context.Context, sandbox *sandboxapi.Sandbox) (*unready, error) {
	pvc, err := r.stateVolume(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	var binding *sandboxapi.StateBinding
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
		pvc, err = r.createClaim(ctx, sandbox, StateVolumeName(sandbox.Name), template.Spec.StateVolume.Claim,
			StateVolumeRecordAnnotation, stateVolumeRecord{Layout: template.Spec.StateVolume.Layout})
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
		recorded.Layout.Type != sandboxapi.DirectoryTree || recorded.Layout.Filesystem != "" {
		return layoutMismatch(fmt.Sprintf("PVC %s has no supported state layout record", pvc.Name)), nil
	}
	if pvc.UID == "" {
		return layoutMismatch("state claim has no UID"), nil
	}
	current := &sandboxapi.StateBinding{Name: pvc.Name, UID: string(pvc.UID), Layout: recorded.Layout}
	if binding != nil && !equality.Semantic.DeepEqual(binding, current) {
		return layoutMismatch("state claim identity or layout differs from its recorded binding"), nil
	}
	if binding == nil {
		if sandbox.Status.Volumes == nil {
			sandbox.Status.Volumes = &sandboxapi.SandboxVolumes{}
		}
		sandbox.Status.Volumes.State = current
		// Persist the identity before a workload can start; a status conflict leaves the PVC intact.
		if err := r.Status().Update(ctx, sandbox); err != nil {
			return nil, err
		}
	}
	return nil, nil
}
