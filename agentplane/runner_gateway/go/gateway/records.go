package gateway

import (
	"context"
	"fmt"
	"math"
	"strconv"
	"time"

	runnercontrolpb "github.com/agentydragon/ducktape/agentplane/proto/runner_control"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	discoveryclient "k8s.io/client-go/kubernetes/typed/discovery/v1"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/util/retry"
)

const (
	connectionLabel           = "agentplane.allegedly.works/runner-connection"
	connectionEpochAnnotation = "agentplane.allegedly.works/connection-epoch"
	podUIDAnnotation          = "agentplane.allegedly.works/pod-uid"
	runnerBootIDAnnotation    = "agentplane.allegedly.works/runner-boot-id"
	quiesceStageAnnotation    = "agentplane.allegedly.works/quiesce-stage"
	idleSinceAnnotation       = "agentplane.allegedly.works/idle-since"
	bootstrapAnnotation       = "agentplane.allegedly.works/bootstrap"
	setupsRunningAnnotation   = "agentplane.allegedly.works/setups-running"
	controlPortName           = "grpc"
	endpointSliceManager      = "agentplane-runner-gateway"
	leaseDurationSeconds      = int32(30)
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
	return &connectionRecords{coordination: coordination, discovery: discovery, namespace: namespace, holder: holder}, nil
}

func (r *connectionRecords) accept(ctx context.Context, sandbox SandboxIdentity, podUID, bootID string) (uint64, error) {
	leaseName := sandbox.Name + "-runner"
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
				Labels:          map[string]string{connectionLabel: "true"},
				Annotations:     map[string]string{podUIDAnnotation: podUID, runnerBootIDAnnotation: bootID},
				OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(sandbox)},
			},
			Spec: coordinationv1.LeaseSpec{
				HolderIdentity:       stringPointer(r.holder.name),
				LeaseDurationSeconds: int32Pointer(leaseDurationSeconds),
				LeaseTransitions:     int32Pointer(next),
				RenewTime:            &metav1.MicroTime{Time: time.Now().UTC()},
			},
		}
		if current == nil {
			_, err = r.coordination.Leases(r.namespace).Create(ctx, lease, metav1.CreateOptions{})
		} else {
			lease.ResourceVersion = current.ResourceVersion
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
	if err := r.pointControlEndpoints(ctx, sandbox, epoch); err != nil {
		return 0, fmt.Errorf("point Sandbox control Service at connection holder: %w", err)
	}
	return epoch, nil
}

func (r *connectionRecords) renew(
	ctx context.Context,
	sandbox SandboxIdentity,
	epoch uint64,
	seenAt time.Time,
	facts *runnerFacts,
) error {
	name := sandbox.Name + "-runner"
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
			!ownsSandbox(lease.OwnerReferences, sandbox.UID) {
			return errSupersededConnection
		}
		if lease.Annotations == nil {
			lease.Annotations = make(map[string]string)
		}
		for _, key := range []string{quiesceStageAnnotation, idleSinceAnnotation, bootstrapAnnotation, setupsRunningAnnotation} {
			delete(lease.Annotations, key)
		}
		lease.Spec.RenewTime = &metav1.MicroTime{Time: seenAt.UTC()}
		if facts != nil {
			annotations, err := facts.annotations()
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

func (r *connectionRecords) pointControlEndpoints(ctx context.Context, sandbox SandboxIdentity, epoch uint64) error {
	name := sandbox.Name + "-control"
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		current, err := r.discovery.EndpointSlices(r.namespace).Get(ctx, name, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			current = nil
		} else if err != nil {
			return err
		}
		if current != nil && ownsSandbox(current.OwnerReferences, sandbox.UID) {
			if currentEpoch, err := strconv.ParseUint(current.Annotations[connectionEpochAnnotation], 10, 64); err == nil && currentEpoch >= epoch {
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
				Annotations:     map[string]string{connectionEpochAnnotation: strconv.FormatUint(epoch, 10)},
				OwnerReferences: []metav1.OwnerReference{sandboxOwnerReference(sandbox)},
			},
			AddressType: discoveryv1.AddressTypeIPv4,
			Endpoints: []discoveryv1.Endpoint{{
				Addresses:  []string{r.holder.ip},
				Conditions: discoveryv1.EndpointConditions{Ready: &ready},
			}},
			Ports: []discoveryv1.EndpointPort{{
				Name: stringPointer(controlPortName), Port: &port, Protocol: protocolPointer(corev1.ProtocolTCP),
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

func sandboxOwnerReference(sandbox SandboxIdentity) metav1.OwnerReference {
	controller, blockOwnerDeletion := false, false
	return metav1.OwnerReference{
		APIVersion: "agentplane.allegedly.works/v1alpha1", Kind: "Sandbox", Name: sandbox.Name, UID: types.UID(sandbox.UID),
		Controller: &controller, BlockOwnerDeletion: &blockOwnerDeletion,
	}
}

func ownsSandbox(references []metav1.OwnerReference, uid string) bool {
	return len(references) == 1 && references[0].Kind == "Sandbox" && string(references[0].UID) == uid
}

func stringPointer(value string) *string                     { return &value }
func int32Pointer(value int32) *int32                        { return &value }
func protocolPointer(value corev1.Protocol) *corev1.Protocol { return &value }

type runnerFacts struct {
	quiesceStage  runnercontrolpb.RunnerFacts_QuiesceStage
	bootstrap     runnercontrolpb.RunnerFacts_Bootstrap
	idleSince     *time.Time
	setupsRunning bool
}

func (f runnerFacts) annotations() (map[string]string, error) {
	annotations := make(map[string]string)
	switch f.quiesceStage {
	case runnercontrolpb.RunnerFacts_QUIESCE_STAGE_UNSPECIFIED:
	case runnercontrolpb.RunnerFacts_QUIESCE_STAGE_DRAINING:
		annotations[quiesceStageAnnotation] = "draining"
	case runnercontrolpb.RunnerFacts_QUIESCE_STAGE_FLUSHING:
		annotations[quiesceStageAnnotation] = "flushing"
	case runnercontrolpb.RunnerFacts_QUIESCE_STAGE_QUIESCED:
		annotations[quiesceStageAnnotation] = "quiesced"
	default:
		return nil, fmt.Errorf("unknown runner quiesce stage %d", f.quiesceStage)
	}
	switch f.bootstrap {
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_UNSPECIFIED:
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_RUNNING:
		annotations[bootstrapAnnotation] = "running"
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_SUCCEEDED:
		annotations[bootstrapAnnotation] = "succeeded"
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_FAILED:
		annotations[bootstrapAnnotation] = "failed"
	case runnercontrolpb.RunnerFacts_BOOTSTRAP_INTERRUPTED:
		annotations[bootstrapAnnotation] = "interrupted"
	default:
		return nil, fmt.Errorf("unknown runner bootstrap state %d", f.bootstrap)
	}
	if f.idleSince != nil {
		annotations[idleSinceAnnotation] = f.idleSince.UTC().Format(time.RFC3339Nano)
	}
	if f.setupsRunning {
		annotations[setupsRunningAnnotation] = "true"
	}
	return annotations, nil
}

func watchConnectionLeases(ctx context.Context, client coordinationclient.CoordinationV1Interface, namespace string, local *connections) error {
	listWatch := &cache.ListWatch{
		ListWithContextFunc: func(ctx context.Context, options metav1.ListOptions) (runtime.Object, error) {
			options.LabelSelector = connectionLabel + "=true"
			return client.Leases(namespace).List(ctx, options)
		},
		WatchFuncWithContext: func(ctx context.Context, options metav1.ListOptions) (watch.Interface, error) {
			options.LabelSelector = connectionLabel + "=true"
			return client.Leases(namespace).Watch(ctx, options)
		},
	}
	informer := cache.NewSharedIndexInformer(listWatch, &coordinationv1.Lease{}, 0, cache.Indexers{})
	leaseUID := func(object any) (string, uint64) {
		lease, ok := object.(*coordinationv1.Lease)
		if !ok || lease.Spec.LeaseTransitions == nil || len(lease.OwnerReferences) != 1 || lease.OwnerReferences[0].Kind != "Sandbox" {
			return "", 0
		}
		return string(lease.OwnerReferences[0].UID), uint64(*lease.Spec.LeaseTransitions)
	}
	if _, err := informer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		AddFunc: func(object any) {
			uid, epoch := leaseUID(object)
			local.replaceLease("", uid, epoch)
		},
		UpdateFunc: func(previous, current any) {
			previousUID, _ := leaseUID(previous)
			uid, epoch := leaseUID(current)
			local.replaceLease(previousUID, uid, epoch)
		},
		DeleteFunc: func(object any) {
			if tombstone, ok := object.(cache.DeletedFinalStateUnknown); ok {
				object = tombstone.Obj
			}
			uid, _ := leaseUID(object)
			local.forgetLease(uid)
		},
	}); err != nil {
		return err
	}
	go informer.Run(ctx.Done())
	if !cache.WaitForCacheSync(ctx.Done(), informer.HasSynced) {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("connection Lease cache did not sync")
	}
	return nil
}
