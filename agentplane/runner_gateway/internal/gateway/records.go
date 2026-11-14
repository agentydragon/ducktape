package gateway

import (
	"context"
	"fmt"
	"math"
	"strconv"
	"sync"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	discoveryclient "k8s.io/client-go/kubernetes/typed/discovery/v1"
	"k8s.io/client-go/util/retry"
)

const (
	endpointSliceManager = "agentplane-runner-gateway"
	leaseDurationSeconds = int32(30)
)

type connectionHolder struct {
	name string
	ip   string
	port int32
}

// CLEANUP(added 2026-11-14): Remove route records after this any-replica image is
// pinned and callers switch from <sandbox>-control to the Gateway Service.
// These records provide legacy routing only, never Runtime call authority.
type connectionRecords struct {
	coordination coordinationclient.CoordinationV1Interface
	discovery    discoveryclient.DiscoveryV1Interface
	namespace    string
	holder       connectionHolder
	now          func() time.Time
	observed     leaseObservations
}

// leaseObservations decides when another replica's connection Lease expires as client-go leaderelection
// does: by how long this replica has seen the same renewal on its own monotonic clock, never by comparing
// the writer's RenewTime with the local clock, so a skewed writer clock neither expires a live claim early
// nor keeps a dead one forever. A replica that has not seen a renewal before waits a full duration.
type leaseObservations struct {
	mu   sync.Mutex
	seen map[string]leaseObservation
}

type leaseObservation struct {
	renewal   leaseRenewal
	firstSeen time.Time
}

// leaseRenewal identifies one renewal; any change shows the holder is alive.
type leaseRenewal struct {
	uid         types.UID
	holder      string
	transitions int32
	renewMicros int64
}

// held reports whether lease's holder still holds it at now: whether this replica first saw its current
// renewal less than its duration ago. The caller has checked that holder, RenewTime and duration are set.
func (o *leaseObservations) held(lease *coordinationv1.Lease, now time.Time) bool {
	renewal := leaseRenewal{
		uid: lease.UID, holder: *lease.Spec.HolderIdentity, renewMicros: lease.Spec.RenewTime.UnixMicro(),
	}
	if lease.Spec.LeaseTransitions != nil {
		renewal.transitions = *lease.Spec.LeaseTransitions
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	observation, ok := o.seen[lease.Name]
	if !ok || observation.renewal != renewal {
		observation = leaseObservation{renewal: renewal, firstSeen: now}
		if o.seen == nil {
			o.seen = map[string]leaseObservation{}
		}
		o.seen[lease.Name] = observation
	}
	return now.Sub(observation.firstSeen) < time.Duration(*lease.Spec.LeaseDurationSeconds)*time.Second
}

func (o *leaseObservations) forget(name string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	delete(o.seen, name)
}

func newConnectionRecords(
	coordination coordinationclient.CoordinationV1Interface,
	discovery discoveryclient.DiscoveryV1Interface,
	namespace string,
	holder connectionHolder,
) (*connectionRecords, error) {
	if coordination == nil || discovery == nil || namespace == "" || holder.name == "" || holder.ip == "" || holder.port < 1 {
		return nil, fmt.Errorf("connection-record clients, namespace and holder are required")
	}
	return &connectionRecords{coordination: coordination, discovery: discovery, namespace: namespace, holder: holder, now: time.Now}, nil
}

func (r *connectionRecords) claim(ctx context.Context, sandbox *sandboxreader.Identity, podUID string) (uint64, types.UID, error) {
	leaseName := sandbox.Name + environmentRuntimeLeaseSuffix
	var epoch uint64
	var leaseUID types.UID
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		current, err := r.coordination.Leases(r.namespace).Get(ctx, leaseName, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			current = nil
		} else if err != nil {
			return err
		}
		routeEpoch, err := r.controlRouteEpoch(ctx, sandbox)
		if err != nil {
			return err
		}
		next := routeEpoch + 1
		if current != nil {
			if !ownsSandbox(current.OwnerReferences, sandbox) || !current.DeletionTimestamp.IsZero() {
				return fmt.Errorf("refusing to adopt Lease %s: foreign or being deleted", leaseName)
			}
			if current.Spec.HolderIdentity != nil && *current.Spec.HolderIdentity != r.holder.name &&
				current.Spec.RenewTime != nil && current.Spec.LeaseDurationSeconds != nil && r.observed.held(current, r.now()) {
				return errRunnerOwned
			}
			if current.Spec.LeaseTransitions != nil {
				if *current.Spec.LeaseTransitions < 0 {
					return fmt.Errorf("connection Lease %s has invalid transition epoch %d", leaseName, *current.Spec.LeaseTransitions)
				}
				if uint64(*current.Spec.LeaseTransitions) >= next {
					next = uint64(*current.Spec.LeaseTransitions) + 1
				}
			}
		}
		if next == 0 || next > math.MaxInt32 {
			return fmt.Errorf("connection epoch exhausted")
		}
		lease := &coordinationv1.Lease{
			TypeMeta: metav1.TypeMeta{APIVersion: "coordination.k8s.io/v1", Kind: "Lease"},
			ObjectMeta: metav1.ObjectMeta{
				Name: leaseName, Namespace: r.namespace,
				Labels:          map[string]string{environmentRuntimeConnectionLabel: "true"},
				Annotations:     map[string]string{podUIDAnnotation: podUID},
				OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(sandbox)},
			},
			Spec: coordinationv1.LeaseSpec{
				HolderIdentity:       stringPointer(r.holder.name),
				LeaseDurationSeconds: int32Pointer(leaseDurationSeconds),
				LeaseTransitions:     int32Pointer(int32(next)),
				RenewTime:            &metav1.MicroTime{Time: r.now().UTC()},
			},
		}
		if current == nil {
			lease, err = r.coordination.Leases(r.namespace).Create(ctx, lease, metav1.CreateOptions{})
		} else {
			lease.ResourceVersion = current.ResourceVersion
			lease.UID = current.UID
			if uid := lastConnectedPodUID(current.Annotations); uid != "" {
				lease.Annotations[lastConnectedPodUIDAnnotation] = uid
			}
			lease, err = r.coordination.Leases(r.namespace).Update(ctx, lease, metav1.UpdateOptions{})
		}
		if err != nil {
			return err
		}
		if lease.UID == "" {
			return fmt.Errorf("connection Lease %s has no UID", leaseName)
		}
		leaseUID = lease.UID
		epoch = next
		return nil
	})
	if err != nil {
		return 0, "", fmt.Errorf("accept runner connection: %w", err)
	}
	r.observed.forget(leaseName)
	if err := r.pointControlEndpoints(ctx, sandbox, leaseUID, epoch); err != nil {
		// The Lease was claimed even if publication failed. Return its identity so
		// the observer can release that exact claim after closing its connection.
		return epoch, leaseUID, fmt.Errorf("point Sandbox control Service at connection holder: %w", err)
	}
	return epoch, leaseUID, nil
}

// release gives up only this exact claim after the observer's local traffic and direct Runner
// connection have stopped. A resource-version update and a fresh holder/UID/epoch check on every
// conflict prevent a delayed release from clearing a successor's claim.
func (r *connectionRecords) release(ctx context.Context, sandbox *sandboxreader.Identity, leaseUID types.UID, epoch uint64) error {
	name := sandbox.Name + environmentRuntimeLeaseSuffix
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		lease, err := r.coordination.Leases(r.namespace).Get(ctx, name, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			return errSupersededConnection
		}
		if err != nil {
			return err
		}
		if !ownsSandbox(lease.OwnerReferences, sandbox) || !lease.DeletionTimestamp.IsZero() || lease.UID != leaseUID ||
			lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != r.holder.name ||
			lease.Spec.LeaseTransitions == nil || uint64(*lease.Spec.LeaseTransitions) != epoch {
			return errSupersededConnection
		}
		lease.Spec.HolderIdentity = nil
		lease.Spec.RenewTime = nil
		delete(lease.Annotations, environmentRuntimeBootIDAnnotation)
		_, err = r.coordination.Leases(r.namespace).Update(ctx, lease, metav1.UpdateOptions{})
		return err
	})
}

func (r *connectionRecords) recordBootID(ctx context.Context, sandbox *sandboxreader.Identity, epoch uint64, bootID string) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		lease, err := r.coordination.Leases(r.namespace).Get(ctx, sandbox.Name+environmentRuntimeLeaseSuffix, metav1.GetOptions{})
		if err != nil {
			return err
		}
		if !ownsSandbox(lease.OwnerReferences, sandbox) || lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != r.holder.name ||
			lease.Spec.LeaseTransitions == nil || uint64(*lease.Spec.LeaseTransitions) != epoch {
			return errSupersededConnection
		}
		if lease.Annotations == nil {
			lease.Annotations = make(map[string]string)
		}
		lease.Annotations[environmentRuntimeBootIDAnnotation] = bootID
		lease.Annotations[lastConnectedPodUIDAnnotation] = lease.Annotations[podUIDAnnotation]
		_, err = r.coordination.Leases(r.namespace).Update(ctx, lease, metav1.UpdateOptions{})
		return err
	})
}

func (r *connectionRecords) renew(
	ctx context.Context,
	sandbox *sandboxreader.Identity,
	epoch uint64,
	seenAt time.Time,
) error {
	name := sandbox.Name + environmentRuntimeLeaseSuffix
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		lease, err := r.coordination.Leases(r.namespace).Get(ctx, name, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			return errSupersededConnection
		}
		if err != nil {
			return err
		}
		if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != r.holder.name ||
			lease.Spec.LeaseTransitions == nil || uint64(*lease.Spec.LeaseTransitions) != epoch ||
			!ownsSandbox(lease.OwnerReferences, sandbox) {
			return errSupersededConnection
		}
		lease.Spec.RenewTime = &metav1.MicroTime{Time: seenAt.UTC()}
		_, err = r.coordination.Leases(r.namespace).Update(ctx, lease, metav1.UpdateOptions{})
		return err
	})
	if err != nil {
		return fmt.Errorf("renew runner connection Lease: %w", err)
	}
	return nil
}

func (r *connectionRecords) controlRouteEpoch(ctx context.Context, sandbox *sandboxreader.Identity) (uint64, error) {
	name := sandbox.Name + sandboxapi.ControlServiceSuffix
	current, err := r.discovery.EndpointSlices(r.namespace).Get(ctx, name, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	if !ownsSandbox(current.OwnerReferences, sandbox) || !current.DeletionTimestamp.IsZero() {
		return 0, fmt.Errorf("refusing to adopt EndpointSlice %s: foreign or being deleted", name)
	}
	routeEpoch, err := parseControlRouteEpoch(current)
	if err != nil {
		return 0, fmt.Errorf("refusing to adopt EndpointSlice %s with invalid connection epoch: %w", name, err)
	}
	if routeEpoch >= math.MaxInt32 {
		return 0, fmt.Errorf("connection epoch exhausted")
	}
	return routeEpoch, nil
}

func (r *connectionRecords) pointControlEndpoints(ctx context.Context, sandbox *sandboxreader.Identity, leaseUID types.UID, epoch uint64) error {
	name := sandbox.Name + sandboxapi.ControlServiceSuffix
	leaseName := sandbox.Name + environmentRuntimeLeaseSuffix
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		lease, err := r.coordination.Leases(r.namespace).Get(ctx, leaseName, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			return errSupersededConnection
		}
		if err != nil {
			return err
		}
		if !ownsSandbox(lease.OwnerReferences, sandbox) || !lease.DeletionTimestamp.IsZero() || lease.UID != leaseUID ||
			lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != r.holder.name ||
			lease.Spec.LeaseTransitions == nil || uint64(*lease.Spec.LeaseTransitions) != epoch {
			return errSupersededConnection
		}
		current, err := r.discovery.EndpointSlices(r.namespace).Get(ctx, name, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			current = nil
		} else if err != nil {
			return err
		}
		if current != nil {
			if !ownsSandbox(current.OwnerReferences, sandbox) || !current.DeletionTimestamp.IsZero() {
				return fmt.Errorf("refusing to adopt EndpointSlice %s: foreign or being deleted", name)
			}
			currentEpoch, err := parseControlRouteEpoch(current)
			if err != nil {
				return fmt.Errorf("refusing to update EndpointSlice %s with invalid connection epoch: %w", name, err)
			}
			if currentEpoch >= epoch {
				return nil
			}
		}
		port := r.holder.port
		ready := true
		slice := &discoveryv1.EndpointSlice{
			TypeMeta: metav1.TypeMeta{APIVersion: "discovery.k8s.io/v1", Kind: "EndpointSlice"},
			ObjectMeta: metav1.ObjectMeta{
				Name: name, Namespace: r.namespace,
				Labels: map[string]string{
					"kubernetes.io/service-name":             name,
					"endpointslice.kubernetes.io/managed-by": endpointSliceManager,
				},
				Annotations: map[string]string{
					connectionEpochAnnotation:    strconv.FormatUint(epoch, 10),
					connectionLeaseUIDAnnotation: string(leaseUID),
				},
				OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(sandbox)},
			},
			AddressType: discoveryv1.AddressTypeIPv4,
			Endpoints: []discoveryv1.Endpoint{{
				Addresses:  []string{r.holder.ip},
				Conditions: discoveryv1.EndpointConditions{Ready: &ready},
			}},
			Ports: []discoveryv1.EndpointPort{{
				Name: stringPointer(sandboxapi.ControlServicePortName), Port: &port, Protocol: protocolPointer(corev1.ProtocolTCP),
			}},
		}
		if current == nil {
			_, err = r.discovery.EndpointSlices(r.namespace).Create(ctx, slice, metav1.CreateOptions{})
		} else {
			slice.ResourceVersion = current.ResourceVersion
			_, err = r.discovery.EndpointSlices(r.namespace).Update(ctx, slice, metav1.UpdateOptions{})
		}
		return err
	})
}

// An EndpointSlice created before connection epochs were recorded has no prior high-water mark.
// Preserve that legacy baseline, but never overwrite a present malformed value.
func parseControlRouteEpoch(slice *discoveryv1.EndpointSlice) (uint64, error) {
	annotation := slice.Annotations[connectionEpochAnnotation]
	if annotation == "" {
		return 0, nil
	}
	return strconv.ParseUint(annotation, 10, 64)
}

func sandboxOwnerReference(sandbox *sandboxreader.Identity) metav1.OwnerReference {
	controller, blockOwnerDeletion := false, false
	return metav1.OwnerReference{
		APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: sandbox.Name, UID: types.UID(sandbox.Uid),
		Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
	}
}

func ownsSandbox(references []metav1.OwnerReference, sandbox *sandboxreader.Identity) bool {
	return len(references) == 1 && references[0].APIVersion == sandboxapi.GroupVersion.String() &&
		references[0].Kind == sandboxapi.SandboxKind && references[0].Name == sandbox.Name &&
		string(references[0].UID) == sandbox.Uid
}

func stringPointer(value string) *string                     { return &value }
func int32Pointer(value int32) *int32                        { return &value }
func protocolPointer(value corev1.Protocol) *corev1.Protocol { return &value }
