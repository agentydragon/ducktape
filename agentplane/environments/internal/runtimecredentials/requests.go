// Package runtimecredentials decides which credential a Sandbox incarnation may use. It receives
// object snapshots and requests WorkloadCredentials; child and Sandbox writes belong to the controller.
package runtimecredentials

import (
	"context"
	"fmt"
	"time"

	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const runtimeCredentialRetry = 5 * time.Second

// Gate is why credentials do not yet permit running or publishing an incarnation.
type Gate struct {
	Reason, Message string
	RetryAfter      time.Duration
}

// Requests uses the controller's snapshots. Identity and Record are nil when absent; Record is
// read uncached. Only Requester writes, and only to WorkloadCredentials.
type Requests struct {
	Requester requester.Requester
	Identity  *policyapi.Identity
	Record    *workloadapi.WorkloadCredential
}

// Owner is the Sandbox as its WorkloadCredential's owner: the record takes the Sandbox's
// name and goes with it.
func Owner(sandbox *sandboxapi.Sandbox) requester.Owner {
	return requester.OwnedBy(sandbox, sandboxapi.GroupVersion.WithKind(sandboxapi.SandboxKind))
}

// RequestStart asks for the unbound credential the Sandbox's next incarnation starts
// with. A record naming another subject is revoked first, since a record's subject changes only
// with a new binding.
func (r Requests) RequestStart(ctx context.Context, sandbox *sandboxapi.Sandbox) (requester.Issued, *Gate, error) {
	subject, changed, blocked := r.credentialSubject(sandbox, false)
	if blocked != nil {
		return requester.Issued{}, blocked, nil
	}
	if changed {
		return requester.Issued{}, credentialPending("IdentityChanged", "the WorkloadCredential subject differs from the Sandbox's Identity and is being revoked"),
			r.Requester.Revoke(ctx, Owner(sandbox))
	}
	return r.requestCredential(ctx, sandbox, subject, nil)
}

// RequestIncarnation asks for the credential bound to the incarnation the Sandbox
// accepts. changed says the incarnation's recorded subject differs from the Sandbox's Identity,
// and must go instead.
func (r Requests) RequestIncarnation(ctx context.Context, sandbox *sandboxapi.Sandbox, binding requester.Binding) (issued requester.Issued, changed bool, pending *Gate, err error) {
	subject, changed, _ := r.credentialSubject(sandbox, true)
	if changed {
		return requester.Issued{}, true, nil, nil
	}
	issued, pending, err = r.requestCredential(ctx, sandbox, subject, binding)
	return issued, false, pending, err
}

// requestCredential asks for the credential the Sandbox's incarnation runs with: binding is the one
// it accepts, or nil before one exists.
func (r Requests) requestCredential(ctx context.Context, sandbox *sandboxapi.Sandbox, subject requester.Identity, binding requester.Binding) (requester.Issued, *Gate, error) {
	credential, err := r.Requester.Ensure(ctx, requester.Request{
		Owner: Owner(sandbox), Subject: subject, Binding: binding,
	})
	if err != nil {
		return requester.Issued{}, nil, err
	}
	switch credential := credential.(type) {
	case requester.Issued:
		return credential, nil, nil
	case requester.Pending:
		return requester.Issued{}, credentialPending(credential.Reason, credential.Message), nil
	default:
		return requester.Issued{}, nil, fmt.Errorf("unknown credential %T", credential)
	}
}

// Identity returns the live subject the Sandbox names, or a gate preventing the next start.
func Identity(sandbox *sandboxapi.Sandbox, identity *policyapi.Identity) (requester.Identity, *Gate) {
	if sandbox.Spec.Identity == nil {
		return requester.Identity{}, &Gate{Reason: "IdentityMissing", Message: "spec.identity names no Identity"}
	}
	if identity == nil || !identity.DeletionTimestamp.IsZero() {
		return requester.Identity{}, &Gate{Reason: "IdentityMissing", Message: fmt.Sprintf("Identity %s does not exist", sandbox.Spec.Identity.Name)}
	}
	return requester.Identity{Name: identity.Name, UID: identity.UID}, nil
}

// credentialSubject is who the Sandbox's WorkloadCredential is requested for. An incarnation keeps
// the Identity its record already names: a running workload never changes identity, even when that
// Identity goes. A non-Identity record is unsupported; changed says a running incarnation must be
// replaced, or an unbound record revoked before it is requested anew. blocked says no incarnation
// may start.
func (r Requests) credentialSubject(sandbox *sandboxapi.Sandbox, incarnation bool) (subject requester.Identity, changed bool, blocked *Gate) {
	desired, missing := Identity(sandbox, r.Identity)
	var recorded requester.Identity
	recordExists := r.Record != nil && metav1.IsControlledBy(r.Record, sandbox)
	if recordExists {
		recorded = recordedSubject(r.Record.Spec.Subject)
	}
	switch {
	case incarnation && recorded.Name != "":
		return recorded, missing == nil && recorded != desired, nil
	case incarnation && recordExists:
		// A Sandbox credential must name an Identity. An owned record with another subject cannot
		// authenticate this incarnation, so replace it instead of carrying a legacy subject.
		return desired, true, nil
	case incarnation:
		// The incarnation's record is gone, so its credential no longer verifies.
		return desired, missing != nil, nil
	case missing != nil:
		return requester.Identity{}, false, missing
	default:
		return desired, recordExists && (recorded.Name == "" || recorded != desired), nil
	}
}

// recordedSubject recognizes the Identity subjects supported for Sandbox credentials.
func recordedSubject(subject workloadapi.Subject) requester.Identity {
	if subject.Identity != nil {
		return requester.Identity{Name: subject.Identity.Name, UID: subject.Identity.UID}
	}
	return requester.Identity{}
}

func credentialPending(reason, message string) *Gate {
	return &Gate{Reason: reason, Message: message, RetryAfter: runtimeCredentialRetry}
}
