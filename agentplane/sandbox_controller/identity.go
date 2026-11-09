package sandboxcontroller

import (
	"context"
	"fmt"

	policyapplyv1 "github.com/agentydragon/ducktape/agentplane/policy/client/applyconfigurations/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/requester"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	corev1ac "k8s.io/client-go/applyconfigurations/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// ActionCallerLabel on a ServiceAccount is what admits its workload to the Action Service. It
// grants nothing alone: the account's PolicyBinding action rules decide. Must equal CALLER_LABEL in
// agentplane/action_service/policies/resources.py and CallerLabel in agentplane/action_service/informer.
const ActionCallerLabel = "agentplane.allegedly.works/use-action-service"

// ensureIdentity keeps the Sandbox's own ServiceAccount and, while it names PolicySets, its one
// PolicyBinding. Both are controlled by the Sandbox, so deleting the Sandbox revokes them. It
// returns why no Pod may start yet; a running Pod is never stopped for it. A Pod does not wait for
// the binding's readers: a grant that lands after the Pod starts only means early requests are
// refused, the same as for a running Pod whose PolicySets change.
func (r *Reconciler) ensureIdentity(ctx context.Context, sandbox *sandboxapi.Sandbox) (*unready, error) {
	account := corev1ac.ServiceAccount(sandbox.Name, sandbox.Namespace).
		WithLabels(map[string]string{sandboxapi.SandboxLabel: sandbox.Name, ActionCallerLabel: "true"})
	if blocked, err := r.applyOwned(ctx, sandbox, account, "ServiceAccountConflict"); blocked != nil || err != nil {
		return blocked, err
	}
	return r.ensureBinding(ctx, sandbox)
}

func (r *Reconciler) ensureBinding(ctx context.Context, sandbox *sandboxapi.Sandbox) (*unready, error) {
	var binding policyapi.PolicyBinding
	err := r.Objects.Get(ctx, client.ObjectKeyFromObject(sandbox), &binding)
	if err != nil && !apierrors.IsNotFound(err) {
		return nil, err
	}
	if err == nil {
		if !metav1.IsControlledBy(&binding, sandbox) || !binding.DeletionTimestamp.IsZero() {
			return &unready{reason: "PolicyBindingConflict", message: fmt.Sprintf("PolicyBinding %s is foreign", binding.Name)}, nil
		}
		if len(sandbox.Spec.PolicySets) == 0 {
			return nil, client.IgnoreNotFound(r.Delete(ctx, &binding, client.Preconditions{UID: &binding.UID}))
		}
		// SSA cannot express removing this non-nullable optional CRD field when another
		// writer owns it. Clear drift with an optimistic patch before applying our intent.
		if binding.Spec.ExpiresAt != nil {
			before := binding.DeepCopy()
			binding.Spec.ExpiresAt = nil
			if err := r.Patch(ctx, &binding, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{})); err != nil {
				return nil, err
			}
		}
	}
	if len(sandbox.Spec.PolicySets) == 0 {
		return nil, nil
	}
	return r.applyOwned(ctx, sandbox, policyapplyv1.PolicyBinding(sandbox.Name, sandbox.Namespace).
		WithLabels(map[string]string{sandboxapi.SandboxLabel: sandbox.Name}).
		WithSpec(policyapplyv1.PolicyBindingSpec().
			WithSubject(policyapplyv1.Subject().WithNamespace(sandbox.Namespace).WithName(sandbox.Name)).
			WithPolicySets(sandbox.Spec.PolicySets...)), "PolicyBindingConflict")
}

// identityGate is why no workload may start in identity mode, or nil: the Sandbox names no live
// Identity, or names PolicySets, which bind the ServiceAccount it no longer has. A running workload
// is never stopped for it; with no workload, nothing runs, which admission reads to allow binding it.
func (r *Reconciler) identityGate(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload) (*unready, error) {
	var blocked *unready
	if len(sandbox.Spec.PolicySets) > 0 {
		blocked = &unready{reason: "PolicySetsUnsupported", message: "spec.policySets binds the Sandbox's own ServiceAccount, which a Sandbox acting as an Identity does not have; bind the Identity instead"}
	} else if _, missing, err := r.identity(ctx, sandbox); err != nil {
		return nil, err
	} else {
		blocked = missing
	}
	if blocked != nil && w.pod == nil && w.vm == nil && w.vmi == nil {
		blocked.stage = sandboxapi.StopStageStopped
	}
	return blocked, nil
}

// identity is the live Identity spec.identity names, or why there is none.
func (r *Reconciler) identity(ctx context.Context, sandbox *sandboxapi.Sandbox) (requester.Identity, *unready, error) {
	if sandbox.Spec.Identity == nil {
		return requester.Identity{}, &unready{reason: "IdentityMissing", message: "spec.identity names no Identity"}, nil
	}
	var identity policyapi.Identity
	err := r.Get(ctx, client.ObjectKey{Namespace: sandbox.Namespace, Name: sandbox.Spec.Identity.Name}, &identity)
	if apierrors.IsNotFound(err) || (err == nil && !identity.DeletionTimestamp.IsZero()) {
		return requester.Identity{}, &unready{reason: "IdentityMissing", message: fmt.Sprintf("Identity %s does not exist", sandbox.Spec.Identity.Name)}, nil
	}
	if err != nil {
		return requester.Identity{}, nil, err
	}
	return requester.Identity{Name: identity.Name, UID: identity.UID}, nil, nil
}

// credentialSubject is who the Sandbox's WorkloadCredential is requested for. An incarnation keeps
// the subject its record already names: a running workload never changes identity, even when its
// Identity goes. changed says the Sandbox now names another identity than the record, so a running
// incarnation must be replaced, or an unbound record revoked before it is requested anew; blocked
// says no incarnation may start.
func (r *Reconciler) credentialSubject(ctx context.Context, sandbox *sandboxapi.Sandbox, incarnation bool) (subject requester.Subject, changed bool, blocked *unready, err error) {
	if !r.Identities {
		return requester.ServiceAccount{Name: sandbox.Name}, false, nil, nil
	}
	desired, missing, err := r.identity(ctx, sandbox)
	if err != nil {
		return nil, false, nil, err
	}
	var record workloadapi.WorkloadCredential
	var recorded requester.Subject
	if err := r.Objects.Get(ctx, client.ObjectKeyFromObject(sandbox), &record); err == nil && metav1.IsControlledBy(&record, sandbox) {
		recorded = recordedSubject(record.Spec.Subject)
	} else if err != nil && !apierrors.IsNotFound(err) {
		return nil, false, nil, err
	}
	switch {
	case incarnation && recorded != nil:
		return recorded, missing == nil && recorded != requester.Subject(desired), nil, nil
	case incarnation:
		// The incarnation's record is gone, so its credential no longer verifies.
		return desired, missing != nil, nil, nil
	case missing != nil:
		return nil, false, missing, nil
	default:
		return desired, recorded != nil && recorded != requester.Subject(desired), nil, nil
	}
}

// recordedSubject is the subject a WorkloadCredential names, as a request names it.
func recordedSubject(subject workloadapi.Subject) requester.Subject {
	if subject.Identity != nil {
		return requester.Identity{Name: subject.Identity.Name, UID: subject.Identity.UID}
	}
	return requester.ServiceAccount{Name: subject.ServiceAccount}
}

// owned is the metadata of an object named after the Sandbox and labeled with it.
func owned(sandbox *sandboxapi.Sandbox) metav1.ObjectMeta {
	return metav1.ObjectMeta{Name: sandbox.Name, Namespace: sandbox.Namespace, Labels: map[string]string{sandboxapi.SandboxLabel: sandbox.Name}}
}
