package dispatcher

import (
	"context"
	"fmt"
	"strings"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/google/uuid"
	coordinationv1 "k8s.io/api/coordination/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const (
	LeaseDuration         = 30 * time.Second
	LeaseRenewInterval    = 10 * time.Second
	leaseConflictAttempts = 5
	leaseLabel            = "agentplane.allegedly.works/session-dispatcher-owner"
)

// LeaseOperations is implemented by the typed Kubernetes Lease client.
type LeaseOperations interface {
	Get(context.Context, string, metav1.GetOptions) (*coordinationv1.Lease, error)
	Create(context.Context, *coordinationv1.Lease, metav1.CreateOptions) (*coordinationv1.Lease, error)
	Update(context.Context, *coordinationv1.Lease, metav1.UpdateOptions) (*coordinationv1.Lease, error)
}

type OwnershipLeases struct {
	Leases                    LeaseOperations
	Store                     *Store
	Namespace, HolderIdentity string
}

func NewOwnershipLeases(leases LeaseOperations, store *Store, namespace string) *OwnershipLeases {
	return &OwnershipLeases{Leases: leases, Store: store, Namespace: namespace, HolderIdentity: uuid.NewString()}
}

func leaseName(sandboxUID string) string {
	return "dispatcher-" + strings.ReplaceAll(sandboxUID, "-", "")
}

func leaseOwns(lease *coordinationv1.Lease, sandboxUID string) bool {
	if lease == nil || len(lease.OwnerReferences) != 1 {
		return false
	}
	ref := lease.OwnerReferences[0]
	return ref.Kind == sandboxapi.SandboxKind && string(ref.UID) == sandboxUID
}

func sameAcquisition(written, fresh *coordinationv1.Lease, sandboxUID, holder string) bool {
	if written == nil || fresh == nil || written.Spec.HolderIdentity == nil || fresh.Spec.HolderIdentity == nil ||
		written.Spec.LeaseTransitions == nil || fresh.Spec.LeaseTransitions == nil {
		return false
	}
	return written.UID == fresh.UID && *written.Spec.HolderIdentity == holder && *fresh.Spec.HolderIdentity == holder &&
		*written.Spec.LeaseTransitions == *fresh.Spec.LeaseTransitions && leaseOwns(fresh, sandboxUID)
}

func (l *OwnershipLeases) Acquire(ctx context.Context, sandboxName, sandboxUID string) (*Owner, error) {
	name := leaseName(sandboxUID)
	for i := 0; i < leaseConflictAttempts; i++ {
		expected, err := l.Store.OwnershipGeneration(ctx, sandboxUID)
		if err != nil {
			return nil, err
		}
		now := time.Now().UTC()
		current, err := l.Leases.Get(ctx, name, metav1.GetOptions{})
		var written *coordinationv1.Lease
		if apierrors.IsNotFound(err) {
			transitions, duration := int32(1), int32(LeaseDuration.Seconds())
			falseValue := false
			created := &coordinationv1.Lease{
				ObjectMeta: metav1.ObjectMeta{
					Name: name, Namespace: l.Namespace, Labels: map[string]string{leaseLabel: "true"},
					OwnerReferences: []metav1.OwnerReference{{
						APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: sandboxName,
						UID: types.UID(sandboxUID), Controller: &falseValue, BlockOwnerDeletion: &falseValue,
					}},
				},
				Spec: coordinationv1.LeaseSpec{
					HolderIdentity: &l.HolderIdentity, LeaseTransitions: &transitions,
					RenewTime: &metav1.MicroTime{Time: now}, LeaseDurationSeconds: &duration,
				},
			}
			written, err = l.Leases.Create(ctx, created, metav1.CreateOptions{})
			if apierrors.IsAlreadyExists(err) {
				continue
			}
			if err != nil {
				return nil, err
			}
		} else {
			if err != nil {
				return nil, err
			}
			if !leaseOwns(current, sandboxUID) {
				return nil, fmt.Errorf("Dispatcher Lease %s is owned by a different Sandbox", name)
			}
			duration := LeaseDuration
			if current.Spec.LeaseDurationSeconds != nil && *current.Spec.LeaseDurationSeconds > 0 {
				duration = time.Duration(*current.Spec.LeaseDurationSeconds) * time.Second
			}
			expired := current.Spec.HolderIdentity == nil || current.Spec.RenewTime == nil ||
				!current.Spec.RenewTime.Time.Add(duration).After(now)
			if current.Spec.HolderIdentity != nil && *current.Spec.HolderIdentity != l.HolderIdentity && !expired {
				return nil, nil
			}
			transitions := int32(1)
			if current.Spec.LeaseTransitions != nil {
				transitions = *current.Spec.LeaseTransitions
				if current.Spec.HolderIdentity == nil || *current.Spec.HolderIdentity != l.HolderIdentity {
					transitions++
				}
			}
			durationSeconds := int32(LeaseDuration.Seconds())
			current.Spec = coordinationv1.LeaseSpec{
				HolderIdentity:   &l.HolderIdentity,
				LeaseTransitions: &transitions, RenewTime: &metav1.MicroTime{Time: now},
				LeaseDurationSeconds: &durationSeconds,
			}
			written, err = l.Leases.Update(ctx, current, metav1.UpdateOptions{})
			if apierrors.IsConflict(err) || apierrors.IsNotFound(err) {
				continue
			}
			if err != nil {
				return nil, err
			}
		}
		fresh, err := l.Leases.Get(ctx, name, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if !sameAcquisition(written, fresh, sandboxUID, l.HolderIdentity) {
			continue
		}
		if fresh.UID == "" {
			return nil, fmt.Errorf("Dispatcher Lease %s has no UID", name)
		}
		owner, err := l.Store.ClaimSandboxOwner(ctx, sandboxUID, string(fresh.UID),
			int(*fresh.Spec.LeaseTransitions), l.HolderIdentity, expected)
		if err != nil {
			return nil, err
		}
		if owner != nil {
			return owner, nil
		}
	}
	return nil, nil
}

// StillOwns validates the live Lease and DB generation before an external wake
// patch. SQL settlements separately lock the DB row as their linearization point.
func (l *OwnershipLeases) StillOwns(ctx context.Context, owner Owner) (bool, error) {
	lease, err := l.Leases.Get(ctx, leaseName(owner.SandboxUID), metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if lease.Spec.HolderIdentity == nil || lease.Spec.LeaseTransitions == nil || lease.Spec.RenewTime == nil {
		return false, nil
	}
	duration := LeaseDuration
	if lease.Spec.LeaseDurationSeconds != nil && *lease.Spec.LeaseDurationSeconds > 0 {
		duration = time.Duration(*lease.Spec.LeaseDurationSeconds) * time.Second
	}
	if string(lease.UID) != owner.LeaseUID || *lease.Spec.HolderIdentity != owner.HolderIdentity ||
		int(*lease.Spec.LeaseTransitions) != owner.LeaseTransitions || !leaseOwns(lease, owner.SandboxUID) ||
		!lease.Spec.RenewTime.Time.Add(duration).After(time.Now().UTC()) {
		return false, nil
	}
	return l.Store.IsCurrentOwner(ctx, owner)
}
