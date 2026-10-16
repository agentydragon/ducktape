package sandboxcontroller

import (
	"context"
	"fmt"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const fieldManager = "agentplane-sandbox-controller"

// applyOwned accepts fresh apply builders or explicit CRD intent, never a fetched object.
// An exclusive create and guarded updates keep field ownership from adopting a foreign child.
func (r *Reconciler) applyOwned(ctx context.Context, sandbox *sandboxapi.Sandbox, intent any, conflictReason string) (*unready, error) {
	// Normalize explicit maps too, including typed slices and integer values in CRD intent.
	fields, err := runtime.DefaultUnstructuredConverter.ToUnstructured(&intent)
	if err != nil {
		return nil, err
	}
	desired := &unstructured.Unstructured{Object: fields}
	if err := controllerutil.SetControllerReference(sandbox, desired, r.Scheme()); err != nil {
		return nil, err
	}
	current := &unstructured.Unstructured{}
	current.SetGroupVersionKind(desired.GroupVersionKind())
	err = r.Objects.Get(ctx, client.ObjectKeyFromObject(desired), current)
	if apierrors.IsNotFound(err) {
		// A concurrent create is retried and checked through the uncached reader.
		return nil, r.Create(ctx, desired, client.FieldOwner(fieldManager))
	}
	if err != nil {
		return nil, err
	}
	owned := metav1.IsControlledBy(current, sandbox)
	// A control Service created by the Gateway has a non-controller Sandbox reference.
	if current.GetKind() == "Service" && metav1.GetControllerOf(current) == nil {
		for _, owner := range current.GetOwnerReferences() {
			owned = owned || (owner.APIVersion == sandboxapi.GroupVersion.String() && owner.Kind == "Sandbox" && owner.Name == sandbox.Name && owner.UID == sandbox.UID)
		}
	}
	if !owned || !current.GetDeletionTimestamp().IsZero() {
		return &unready{reason: conflictReason, message: fmt.Sprintf("%s %s is foreign or being deleted", current.GetKind(), current.GetName())}, nil
	}
	// Both guards bind the ownership decision to the exact observed object version.
	desired.SetUID(current.GetUID())
	desired.SetResourceVersion(current.GetResourceVersion())
	return nil, r.Apply(ctx, client.ApplyConfigurationFromUnstructured(desired), client.FieldOwner(fieldManager), client.ForceOwnership)
}
