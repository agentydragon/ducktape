// Package requester is how an owner asks for the credential one of its workloads runs with. It
// writes the owner's WorkloadCredential and reads back what the workload-identity component issued;
// it never creates a Certificate.
package requester

import (
	"context"
	"errors"
	"fmt"

	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Requester declares the credential one owner's workload runs with and reports what is issued.
// The Sandbox Controller and the Action Service call it; cdk8s renders the same object for Flux.
type Requester interface {
	// Ensure creates or updates the owner's WorkloadCredential and returns its current status:
	// pending until cert-manager is Ready, then the credential ID, URI and Secret name to mount.
	// Binding names the incarnation (Pod or VMI UID) the owner accepts, or none for a standing
	// workload. A credential ID lives exactly as long as the binding it was accepted for: when
	// Binding leaves it, the component mints a fresh ID, and never otherwise.
	Ensure(ctx context.Context, request Request) (Credential, error)
	// Revoke deletes the owner's WorkloadCredential; the next request presenting it fails.
	Revoke(ctx context.Context, owner Owner) error
}

// Subject is who the workload acts as: a ServiceAccount until IDENTITY_CUTOVER, then an Identity.
type Subject interface{ isSubject() }

// ServiceAccount names a ServiceAccount in the owner's namespace.
type ServiceAccount struct{ Name string }

// Identity names an Identity in the policy namespace by name and UID.
type Identity struct {
	Name string
	UID  types.UID
}

func (ServiceAccount) isSubject() {}
func (Identity) isSubject()       {}

// Binding is the incarnation an owner accepts. A nil Binding is none.
type Binding interface{ isBinding() }

// Pod binds a Pod incarnation, including a Firecracker launcher Pod; it is also the caller's Pod.
type Pod struct {
	Name string
	UID  types.UID
}

// VMI binds a KubeVirt VirtualMachineInstance incarnation. Launcher, its virt-launcher Pod, is the
// caller's Pod; it is named once KubeVirt has created it, and stays for the VMI's life.
type VMI struct {
	UID      types.UID
	Launcher *Pod
}

func (Pod) isBinding() {}
func (VMI) isBinding() {}

// Owner is the object a WorkloadCredential belongs to: the record is Namespace/Name and carries
// Reference as its controller owner reference, so it goes when the owner does. A record usually
// takes its owner's name (OwnedBy); an owner of several workloads names each record after its
// workload, as the Action Service names a box's after the box.
type Owner struct {
	Namespace, Name string
	Reference       metav1.OwnerReference
}

// OwnedBy names owner, of kind gvk, as the controller of the record of its own name.
func OwnedBy(owner metav1.Object, gvk schema.GroupVersionKind) Owner {
	return Owner{Namespace: owner.GetNamespace(), Name: owner.GetName(), Reference: *metav1.NewControllerRef(owner, gvk)}
}

// Request is what an owner asks for.
type Request struct {
	Owner   Owner
	Subject Subject
	Binding Binding
}

// Credential is the outcome of Ensure: Pending or Issued.
type Credential interface{ isCredential() }

// Pending means the component has not issued for the record's current spec yet, or cannot. Reason
// and Message say why, for the owner's own conditions.
type Pending struct{ Reason, Message string }

// Issued is the credential the component issued for the record's current spec.
type Issued struct {
	ID, URI, SecretName string
	// Accepted is true when the request's binding is the one the component accepted for ID.
	Accepted bool
}

func (Pending) isCredential() {}
func (Issued) isCredential()  {}

type requester struct {
	reader client.Reader
	writer client.Writer
}

// New returns a Requester. reader must read the API server directly: a cached record can show
// the status of a spec this requester has since replaced.
func New(reader client.Reader, writer client.Writer) (Requester, error) {
	if reader == nil || writer == nil {
		return nil, errors.New("WorkloadCredential reader and writer are required")
	}
	return &requester{reader: reader, writer: writer}, nil
}

func (r *requester) Ensure(ctx context.Context, request Request) (Credential, error) {
	spec, err := specFor(request)
	if err != nil {
		return nil, err
	}
	owner := request.Owner
	key := types.NamespacedName{Namespace: owner.Namespace, Name: owner.Name}
	var record workloadapi.WorkloadCredential
	err = r.reader.Get(ctx, key, &record)
	if apierrors.IsNotFound(err) {
		record = workloadapi.WorkloadCredential{
			ObjectMeta: metav1.ObjectMeta{
				Namespace: key.Namespace, Name: key.Name, OwnerReferences: []metav1.OwnerReference{owner.Reference},
			},
			Spec: spec,
		}
		if err := r.writer.Create(ctx, &record); err != nil && !apierrors.IsAlreadyExists(err) {
			return nil, fmt.Errorf("create WorkloadCredential %s: %w", key, err)
		}
		return Pending{Reason: "Requested", Message: fmt.Sprintf("WorkloadCredential %s is waiting for issuance", key)}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read WorkloadCredential %s: %w", key, err)
	}
	if !record.DeletionTimestamp.IsZero() {
		return Pending{Reason: "RecordDeleting", Message: fmt.Sprintf("WorkloadCredential %s is being deleted", key)}, nil
	}
	controller := metav1.GetControllerOfNoCopy(&record)
	if controller == nil || controller.UID != owner.Reference.UID {
		if controller != nil && controller.APIVersion == owner.Reference.APIVersion && controller.Kind == owner.Reference.Kind &&
			controller.Name == owner.Reference.Name {
			// An earlier owner of the same name is gone, since this one holds the name now, and
			// the garbage collector has yet to remove its record.
			uid := record.UID
			if err := r.writer.Delete(ctx, &record, client.Preconditions{UID: &uid}); err != nil && !apierrors.IsNotFound(err) {
				return nil, fmt.Errorf("delete predecessor's WorkloadCredential %s: %w", key, err)
			}
			return Pending{Reason: "PredecessorDeleting", Message: fmt.Sprintf("WorkloadCredential %s belonged to an earlier %s and is being deleted", key, controller.Kind)}, nil
		}
		return Pending{Reason: "RecordConflict", Message: fmt.Sprintf("WorkloadCredential %s belongs to another owner", key)}, nil
	}
	if !equality.Semantic.DeepEqual(record.Spec, spec) {
		record.Spec = spec
		if err := r.writer.Update(ctx, &record); err != nil {
			if apierrors.IsConflict(err) {
				return Pending{Reason: "RecordUpdating", Message: fmt.Sprintf("WorkloadCredential %s changed while being updated", key)}, nil
			}
			return nil, fmt.Errorf("update WorkloadCredential %s: %w", key, err)
		}
		return Pending{Reason: "Requested", Message: fmt.Sprintf("WorkloadCredential %s is waiting for issuance", key)}, nil
	}
	return credentialOf(&record), nil
}

func (r *requester) Revoke(ctx context.Context, owner Owner) error {
	key := types.NamespacedName{Namespace: owner.Namespace, Name: owner.Name}
	var record workloadapi.WorkloadCredential
	if err := r.reader.Get(ctx, key, &record); apierrors.IsNotFound(err) {
		return nil
	} else if err != nil {
		return fmt.Errorf("read WorkloadCredential %s: %w", key, err)
	}
	if controller := metav1.GetControllerOfNoCopy(&record); controller == nil || controller.UID != owner.Reference.UID {
		return nil
	}
	uid := record.UID
	if err := r.writer.Delete(ctx, &record, client.Preconditions{UID: &uid}); err != nil && !apierrors.IsNotFound(err) {
		return fmt.Errorf("delete WorkloadCredential %s: %w", key, err)
	}
	return nil
}

func credentialOf(record *workloadapi.WorkloadCredential) Credential {
	status := record.Status
	if status.ObservedGeneration != record.Generation {
		return Pending{Reason: "Requested", Message: fmt.Sprintf("WorkloadCredential %s is waiting for issuance", record.Name)}
	}
	ready := meta.FindStatusCondition(status.Conditions, workloadapi.ConditionReady)
	if ready == nil {
		return Pending{Reason: "Requested", Message: fmt.Sprintf("WorkloadCredential %s is waiting for issuance", record.Name)}
	}
	if ready.Status != metav1.ConditionTrue {
		return Pending{Reason: ready.Reason, Message: ready.Message}
	}
	return Issued{
		ID: status.CredentialID, URI: status.URI, SecretName: status.SecretName,
		Accepted: record.Spec.Binding != nil && equality.Semantic.DeepEqual(status.AcceptedBinding, record.Spec.Binding),
	}
}

func specFor(request Request) (workloadapi.WorkloadCredentialSpec, error) {
	if request.Owner.Namespace == "" || request.Owner.Name == "" || request.Owner.Reference.Name == "" || request.Owner.Reference.UID == "" ||
		request.Owner.Reference.Controller == nil || !*request.Owner.Reference.Controller {
		return workloadapi.WorkloadCredentialSpec{}, errors.New("a WorkloadCredential request needs a record name and a controller owner with a namespace, name and UID")
	}
	var spec workloadapi.WorkloadCredentialSpec
	switch subject := request.Subject.(type) {
	case ServiceAccount:
		spec.Subject.ServiceAccount = subject.Name
	case Identity:
		spec.Subject.Identity = &workloadapi.IdentitySubject{Name: subject.Name, UID: subject.UID}
	default:
		return workloadapi.WorkloadCredentialSpec{}, fmt.Errorf("unknown subject %T", request.Subject)
	}
	switch binding := request.Binding.(type) {
	case nil:
	case Pod:
		spec.Binding = &workloadapi.Binding{PodUID: binding.UID}
		spec.Pod = &workloadapi.PodRef{Name: binding.Name, UID: binding.UID}
	case VMI:
		spec.Binding = &workloadapi.Binding{VMIUID: binding.UID}
		if binding.Launcher != nil {
			spec.Pod = &workloadapi.PodRef{Name: binding.Launcher.Name, UID: binding.Launcher.UID}
		}
	default:
		return workloadapi.WorkloadCredentialSpec{}, fmt.Errorf("unknown binding %T", request.Binding)
	}
	return spec, nil
}
