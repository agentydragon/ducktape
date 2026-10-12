package sandboxcontroller

import (
	"context"
	"fmt"
	"slices"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

// BindingReaders are the PolicyBinding conditions that must all be True for the binding's current
// generation before a Pod starts: the Policy Controller's, the egress proxy's and the Action
// Service's. Each reader sets its condition on every binding, with or without rules of its type.
var BindingReaders = []string{policycontroller.KubernetesReady, "EgressReady", "ActionsReady"}

// ensureIdentity keeps the Sandbox's own ServiceAccount and, while it names PolicySets, its one
// PolicyBinding. Both are controlled by the Sandbox, so deleting the Sandbox revokes them. It
// returns why no Pod may start yet; a running Pod is never stopped for it.
func (r *Reconciler) ensureIdentity(ctx context.Context, sandbox *Sandbox) (*unready, error) {
	account := &corev1.ServiceAccount{ObjectMeta: owned(sandbox)}
	if err := r.Get(ctx, client.ObjectKeyFromObject(account), account); err != nil {
		if !apierrors.IsNotFound(err) {
			return nil, err
		}
		if err := r.createOwned(ctx, sandbox, account); err != nil {
			return nil, err
		}
	} else if !metav1.IsControlledBy(account, sandbox) {
		return &unready{"ServiceAccountConflict", fmt.Sprintf("ServiceAccount %s exists and is not this Sandbox's", account.Name)}, nil
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
		if err := r.createOwned(ctx, sandbox, binding); err != nil {
			return nil, err
		}
		return &unready{"PolicyBindingNotReady", fmt.Sprintf("PolicyBinding %s is created; its readers have not reported", binding.Name)}, nil
	}
	if !metav1.IsControlledBy(binding, sandbox) {
		return &unready{"PolicyBindingConflict", fmt.Sprintf("PolicyBinding %s exists and is not this Sandbox's", binding.Name)}, nil
	}
	if len(sets) == 0 {
		return nil, client.IgnoreNotFound(r.Delete(ctx, binding, client.Preconditions{UID: &binding.UID}))
	}
	if binding.Spec.Subject != spec.Subject || !slices.Equal(binding.Spec.PolicySets, spec.PolicySets) {
		binding.Spec.Subject, binding.Spec.PolicySets = spec.Subject, spec.PolicySets
		if err := r.Update(ctx, binding); err != nil {
			return nil, err
		}
		// The update bumped the generation, so every reader's condition describes the old sets.
		return &unready{"PolicyBindingNotReady", fmt.Sprintf("PolicyBinding %s is updated; its readers have not reported", binding.Name)}, nil
	}
	var waiting []string
	for _, reader := range BindingReaders {
		condition := meta.FindStatusCondition(binding.Status.Conditions, reader)
		if condition == nil || condition.Status != metav1.ConditionTrue || condition.ObservedGeneration != binding.Generation {
			waiting = append(waiting, reader)
		}
	}
	if waiting != nil {
		return &unready{"PolicyBindingNotReady", fmt.Sprintf("PolicyBinding %s waits on %s", binding.Name, strings.Join(waiting, ", "))}, nil
	}
	return nil, nil
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
