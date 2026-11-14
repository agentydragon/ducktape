package sandboxcontroller

import (
	"context"
	"encoding/json"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// createClaim records layout with the claim, before its identity is persisted on the Sandbox.
func (r *Reconciler) createClaim(ctx context.Context, sandbox *sandboxapi.Sandbox, name string, spec corev1.PersistentVolumeClaimSpec, annotation string, record any) (*corev1.PersistentVolumeClaim, error) {
	encoded, err := json.Marshal(record)
	if err != nil {
		return nil, err
	}
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: sandbox.Namespace,
			Labels:      map[string]string{sandboxapi.SandboxLabel: sandbox.Name},
			Annotations: map[string]string{annotation: string(encoded)},
		},
		Spec: *spec.DeepCopy(),
	}
	if err := controllerutil.SetControllerReference(sandbox, pvc, r.Scheme(), controllerutil.WithBlockOwnerDeletion(false)); err != nil {
		return nil, err
	}
	if err := r.Create(ctx, pvc); err != nil {
		if !apierrors.IsAlreadyExists(err) {
			return nil, err
		}
		// The uncached read exposes foreign collisions and observes the API-assigned UID.
		return r.claim(ctx, sandbox, name)
	}
	return pvc, nil
}
