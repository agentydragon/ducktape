package gateway

import (
	"context"
	"fmt"
	"math"
	"strconv"
	"sync"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	coordinationinformers "k8s.io/client-go/informers/coordination/v1"
	"k8s.io/client-go/kubernetes"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	discoveryclient "k8s.io/client-go/kubernetes/typed/discovery/v1"
	"k8s.io/client-go/tools/cache"
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

func (r *connectionRecords) claim(ctx context.Context, sandbox sandboxreader.Identity, podUID string) (uint64, error) {
	leaseName := sandbox.Name + sandboxlease.RunnerLeaseSuffix
	var epoch uint64
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		current, err := r.coordination.Leases(r.namespace).Get(ctx, leaseName, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			current = nil
		} else if err != nil {
			return err
		}
		var next int32 = 1
		if current != nil {
			if !ownsSandbox(current.OwnerReferences, sandbox) || !current.DeletionTimestamp.IsZero() {
				return fmt.Errorf("refusing to adopt Lease %s: foreign or being deleted", leaseName)
			}
			if current.Spec.HolderIdentity != nil && *current.Spec.HolderIdentity != r.holder.name &&
				current.Spec.RenewTime != nil && current.Spec.LeaseDurationSeconds != nil && r.observed.held(current, r.now()) {
				return errRunnerOwned
			}
			if current.Spec.LeaseTransitions != nil {
				if *current.Spec.LeaseTransitions == math.MaxInt32 {
					return fmt.Errorf("connection epoch exhausted")
				}
				next = *current.Spec.LeaseTransitions + 1
			} else {
				next = 1
			}
		}
		lease := &coordinationv1.Lease{
			TypeMeta: metav1.TypeMeta{APIVersion: "coordination.k8s.io/v1", Kind: "Lease"},
			ObjectMeta: metav1.ObjectMeta{
				Name: leaseName, Namespace: r.namespace,
				Labels:          map[string]string{sandboxlease.ConnectionLabel: "true"},
				Annotations:     map[string]string{sandboxlease.PodUIDAnnotation: podUID},
				OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(sandbox)},
			},
			Spec: coordinationv1.LeaseSpec{
				HolderIdentity:       stringPointer(r.holder.name),
				LeaseDurationSeconds: int32Pointer(leaseDurationSeconds),
				LeaseTransitions:     int32Pointer(next),
				RenewTime:            &metav1.MicroTime{Time: r.now().UTC()},
			},
		}
		if current == nil {
			_, err = r.coordination.Leases(r.namespace).Create(ctx, lease, metav1.CreateOptions{})
		} else {
			lease.ResourceVersion = current.ResourceVersion
			if uid := sandboxlease.LastConnectedPodUID(current.Annotations); uid != "" {
				lease.Annotations[sandboxlease.LastConnectedPodUIDAnnotation] = uid
			}
			_, err = r.coordination.Leases(r.namespace).Update(ctx, lease, metav1.UpdateOptions{})
		}
		if err != nil {
			return err
		}
		epoch = uint64(next)
		return nil
	})
	if err != nil {
		return 0, fmt.Errorf("accept runner connection: %w", err)
	}
	r.observed.forget(leaseName)
	if err := r.pointControlEndpoints(ctx, sandbox, epoch); err != nil {
		return 0, fmt.Errorf("point Sandbox control Service at connection holder: %w", err)
	}
	return epoch, nil
}

func (r *connectionRecords) recordBootID(ctx context.Context, sandbox sandboxreader.Identity, epoch uint64, bootID string) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		lease, err := r.coordination.Leases(r.namespace).Get(ctx, sandbox.Name+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
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
		lease.Annotations[sandboxlease.RunnerBootIDAnnotation] = bootID
		lease.Annotations[sandboxlease.LastConnectedPodUIDAnnotation] = lease.Annotations[sandboxlease.PodUIDAnnotation]
		_, err = r.coordination.Leases(r.namespace).Update(ctx, lease, metav1.UpdateOptions{})
		return err
	})
}

func (r *connectionRecords) renew(
	ctx context.Context,
	sandbox sandboxreader.Identity,
	epoch uint64,
	seenAt time.Time,
	facts *sandboxlease.RunnerFacts,
) error {
	name := sandbox.Name + sandboxlease.RunnerLeaseSuffix
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
		if lease.Annotations == nil {
			lease.Annotations = make(map[string]string)
		}
		for _, key := range []string{
			sandboxlease.QuiesceStageAnnotation, sandboxlease.IdleSinceAnnotation, sandboxlease.BootstrapAnnotation,
			sandboxlease.SetupsRunningAnnotation, sandboxlease.AutosuspendBlockersAnnotation,
		} {
			delete(lease.Annotations, key)
		}
		lease.Spec.RenewTime = &metav1.MicroTime{Time: seenAt.UTC()}
		if facts != nil {
			annotations, err := facts.Annotations()
			if err != nil {
				return err
			}
			for key, value := range annotations {
				lease.Annotations[key] = value
			}
		}
		_, err = r.coordination.Leases(r.namespace).Update(ctx, lease, metav1.UpdateOptions{})
		return err
	})
	if err != nil {
		return fmt.Errorf("renew runner connection Lease: %w", err)
	}
	return nil
}

func (r *connectionRecords) pointControlEndpoints(ctx context.Context, sandbox sandboxreader.Identity, epoch uint64) error {
	name := sandbox.Name + sandboxapi.ControlServiceSuffix
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
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
			if currentEpoch, err := strconv.ParseUint(current.Annotations[sandboxlease.ConnectionEpochAnnotation], 10, 64); err == nil && currentEpoch >= epoch {
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
				Annotations:     map[string]string{sandboxlease.ConnectionEpochAnnotation: strconv.FormatUint(epoch, 10)},
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

func sandboxOwnerReference(sandbox sandboxreader.Identity) metav1.OwnerReference {
	controller, blockOwnerDeletion := false, false
	return metav1.OwnerReference{
		APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: sandbox.Name, UID: types.UID(sandbox.UID),
		Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
	}
}

func ownsSandbox(references []metav1.OwnerReference, sandbox sandboxreader.Identity) bool {
	return len(references) == 1 && references[0].APIVersion == sandboxapi.GroupVersion.String() &&
		references[0].Kind == sandboxapi.SandboxKind && references[0].Name == sandbox.Name &&
		string(references[0].UID) == sandbox.UID
}

func stringPointer(value string) *string                     { return &value }
func int32Pointer(value int32) *int32                        { return &value }
func protocolPointer(value corev1.Protocol) *corev1.Protocol { return &value }

func watchConnectionLeases(ctx context.Context, client kubernetes.Interface, namespace string, local *connections) error {
	informer := coordinationinformers.NewTypedFilteredLeaseInformer(client, namespace, 0, nil, func(options *metav1.ListOptions) {
		options.LabelSelector = sandboxlease.ConnectionLabel + "=true"
	})
	leaseUID := func(lease *coordinationv1.Lease) (string, uint64) {
		if lease == nil || lease.Spec.LeaseTransitions == nil || len(lease.OwnerReferences) != 1 || lease.OwnerReferences[0].Kind != sandboxapi.SandboxKind {
			return "", 0
		}
		return string(lease.OwnerReferences[0].UID), uint64(*lease.Spec.LeaseTransitions)
	}
	registration, err := informer.AddTypedEventHandler(coordinationinformers.LeaseHandlerFuncs{
		AddFunc: func(lease *coordinationv1.Lease) {
			uid, epoch := leaseUID(lease)
			local.replaceLease("", uid, epoch)
		},
		UpdateFunc: func(previous, current *coordinationv1.Lease) {
			previousUID, _ := leaseUID(previous)
			uid, epoch := leaseUID(current)
			local.replaceLease(previousUID, uid, epoch)
		},
		DeleteFunc: func(deleted coordinationinformers.DeletedLease) {
			uid, _ := leaseUID(deleted.OptionalObj)
			local.forgetLease(uid)
		},
	})
	if err != nil {
		return err
	}
	go informer.Run(ctx.Done())
	if !cache.WaitForCacheSync(ctx.Done(), informer.HasSynced, registration.HasSynced) {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("connection Lease cache did not sync")
	}
	return nil
}
