package sandboxcontroller

import (
	"context"
	"fmt"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/requester"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// identityGate is why no workload may start, or nil: the Sandbox names no live Identity. A running workload
// is never stopped for it; with no workload, nothing runs, which admission reads to allow binding it.
func (r *Reconciler) identityGate(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload) (*unready, error) {
	_, blocked, err := r.identity(ctx, sandbox)
	if err != nil {
		return nil, err
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
// the Identity its record already names: a running workload never changes identity, even when that
// Identity goes. A non-Identity record is unsupported; changed says a running incarnation must be
// replaced, or an unbound record revoked before it is requested anew. blocked says no incarnation
// may start.
func (r *Reconciler) credentialSubject(ctx context.Context, sandbox *sandboxapi.Sandbox, incarnation bool) (subject requester.Subject, changed bool, blocked *unready, err error) {
	desired, missing, err := r.identity(ctx, sandbox)
	if err != nil {
		return nil, false, nil, err
	}
	var record workloadapi.WorkloadCredential
	var recorded requester.Subject
	recordExists := false
	if err := r.Objects.Get(ctx, client.ObjectKeyFromObject(sandbox), &record); err == nil && metav1.IsControlledBy(&record, sandbox) {
		recordExists = true
		recorded = recordedSubject(record.Spec.Subject)
	} else if err != nil && !apierrors.IsNotFound(err) {
		return nil, false, nil, err
	}
	switch {
	case incarnation && recorded != nil:
		return recorded, missing == nil && recorded != requester.Subject(desired), nil, nil
	case incarnation && recordExists:
		// A Sandbox credential must name an Identity. An owned record with another subject cannot
		// authenticate this incarnation, so replace it instead of carrying a ServiceAccount subject.
		return desired, true, nil, nil
	case incarnation:
		// The incarnation's record is gone, so its credential no longer verifies.
		return desired, missing != nil, nil, nil
	case missing != nil:
		return nil, false, missing, nil
	default:
		return desired, recordExists && (recorded == nil || recorded != requester.Subject(desired)), nil, nil
	}
}

// recordedSubject recognizes the Identity subjects supported for Sandbox credentials.
func recordedSubject(subject workloadapi.Subject) requester.Subject {
	if subject.Identity != nil {
		return requester.Identity{Name: subject.Identity.Name, UID: subject.Identity.UID}
	}
	return nil
}

// owned is the metadata of an object named after the Sandbox and labeled with it.
func owned(sandbox *sandboxapi.Sandbox) metav1.ObjectMeta {
	return metav1.ObjectMeta{Name: sandbox.Name, Namespace: sandbox.Namespace, Labels: map[string]string{sandboxapi.SandboxLabel: sandbox.Name}}
}
