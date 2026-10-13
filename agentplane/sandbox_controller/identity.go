package sandboxcontroller

import (
	"context"
	"fmt"
	"slices"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

// ActionCallerLabel on a ServiceAccount is what admits its workload to the Action Service. It
// grants nothing alone: the account's PolicyBinding action rules decide. Must equal CALLER_LABEL in
// agentplane/action_service/policies/resources.py.
const ActionCallerLabel = "agentplane.allegedly.works/use-action-service"

// ensureIdentity keeps the Sandbox's own ServiceAccount and, while it names PolicySets, its one
// PolicyBinding. Both are controlled by the Sandbox, so deleting the Sandbox revokes them. It
// returns why no Pod may start yet; a running Pod is never stopped for it. A Pod does not wait for
// the binding's readers: a grant that lands after the Pod starts only means early requests are
// refused, the same as for a running Pod whose PolicySets change.
func (r *Reconciler) ensureIdentity(ctx context.Context, sandbox *Sandbox) (*unready, error) {
	account := &corev1.ServiceAccount{ObjectMeta: owned(sandbox)}
	account.Labels[ActionCallerLabel] = "true"
	if err := r.Get(ctx, client.ObjectKeyFromObject(account), account); err != nil {
		if !apierrors.IsNotFound(err) {
			return nil, err
		}
		if err := r.createOwned(ctx, sandbox, account); err != nil {
			return nil, err
		}
	} else if !metav1.IsControlledBy(account, sandbox) {
		return &unready{reason: "ServiceAccountConflict", message: fmt.Sprintf("ServiceAccount %s exists and is not this Sandbox's", account.Name)}, nil
	} else if account.Labels[ActionCallerLabel] != "true" {
		// An account created before the label was set.
		if account.Labels == nil {
			account.Labels = map[string]string{}
		}
		account.Labels[ActionCallerLabel] = "true"
		if err := r.Update(ctx, account); err != nil {
			return nil, err
		}
	}
	return r.ensureBinding(ctx, sandbox)
}

func (r *Reconciler) ensureBinding(ctx context.Context, sandbox *Sandbox) (*unready, error) {
	sets := sandbox.Spec.PolicySets
	spec := policycontroller.PolicyBindingSpec{
		Subject:    policycontroller.ServiceAccountRef{Namespace: sandbox.Namespace, Name: sandbox.Name},
		PolicySets: slices.Clone(sets),
	}
	binding := &policycontroller.PolicyBinding{ObjectMeta: owned(sandbox)}
	if err := r.Get(ctx, client.ObjectKeyFromObject(binding), binding); err != nil {
		if !apierrors.IsNotFound(err) || len(sets) == 0 {
			return nil, client.IgnoreNotFound(err)
		}
		binding.Spec = spec
		return nil, r.createOwned(ctx, sandbox, binding)
	}
	if !metav1.IsControlledBy(binding, sandbox) {
		return &unready{reason: "PolicyBindingConflict", message: fmt.Sprintf("PolicyBinding %s exists and is not this Sandbox's", binding.Name)}, nil
	}
	if len(sets) == 0 {
		return nil, client.IgnoreNotFound(r.Delete(ctx, binding, client.Preconditions{UID: &binding.UID}))
	}
	if binding.Spec.Subject == spec.Subject && slices.Equal(binding.Spec.PolicySets, spec.PolicySets) {
		return nil, nil
	}
	binding.Spec.Subject, binding.Spec.PolicySets = spec.Subject, spec.PolicySets
	return nil, r.Update(ctx, binding)
}

// owned is the metadata of an object named after the Sandbox and labeled with it.
func owned(sandbox *Sandbox) metav1.ObjectMeta {
	return metav1.ObjectMeta{Name: sandbox.Name, Namespace: sandbox.Namespace, Labels: map[string]string{SandboxLabel: sandbox.Name}}
}

// createOwned creates obj controlled by the Sandbox. An AlreadyExists is a concurrent create, and
// the object's watch event runs the reconcile that checks it.
func (r *Reconciler) createOwned(ctx context.Context, sandbox *Sandbox, obj client.Object) error {
	if err := controllerutil.SetControllerReference(sandbox, obj, r.Scheme()); err != nil {
		return err
	}
	if err := r.Create(ctx, obj); err != nil && !apierrors.IsAlreadyExists(err) {
		return err
	}
	return nil
}
