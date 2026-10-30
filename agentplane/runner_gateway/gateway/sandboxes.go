package gateway

import (
	"context"
	"fmt"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	sandboxVMIAPIVer = "kubevirt.io/v1"
	sandboxVMIKind   = "VirtualMachineInstance"
	phaseRunning     = "Running"
	phaseQuiescing   = "Quiescing"
	modeSuspended    = "Suspended"
	phaseCreate      = "Creating"
	phaseSuspend     = "Suspending"
	phaseTearDown    = "TearingDown"
)

type sandboxView struct {
	identity    sandboxreader.Identity
	phase       string
	incarnation *sandboxapi.Incarnation
}

// sandboxResolver names the Sandbox and runner Pod a request is for, and admits it only while the Pod serves it.
// It reads objects, the informer cache in production, and admits nothing while health says the cache may
// not be acted on.
type sandboxResolver struct {
	namespace string
	sandboxes *sandboxreader.Reader
	objects   client.Reader
	health    *watchhealth.Health
}

func newSandboxResolver(namespace string, objects client.Reader, health *watchhealth.Health) (*sandboxResolver, error) {
	if health == nil {
		return nil, fmt.Errorf("Kubernetes cache health is required")
	}
	sandboxes, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	return &sandboxResolver{namespace: namespace, sandboxes: sandboxes, objects: objects, health: health}, nil
}

func routingView(view sandboxreader.View) sandboxView {
	return sandboxView{identity: view.Identity, phase: sandboxPhase(view.Resource), incarnation: view.Resource.Status.Incarnation}
}

func unavailable(err error) error {
	if err == nil {
		return nil
	}
	if rpcStatus, ok := status.FromError(err); ok && rpcStatus.Code() != codes.Unknown {
		return err
	}
	return status.Error(codes.Unavailable, "Sandbox or runner routing is unavailable")
}

func sandboxPhase(resource *sandboxapi.Sandbox) string {
	stage := resource.Status.StopStage
	incarnated := resource.Status.Incarnation != nil
	if resource.DeletionTimestamp != nil && !resource.DeletionTimestamp.IsZero() {
		if meta.IsStatusConditionTrue(resource.Status.Conditions, sandboxapi.ConditionQuiesced) || stage == sandboxapi.StopStageDeleting {
			return phaseTearDown
		}
		return phaseQuiescing
	}
	if resource.Status.OperatingMode == "" {
		return ""
	}
	if resource.Status.OperatingMode == sandboxapi.OperatingModeRunning {
		if incarnated {
			return phaseRunning
		}
		return phaseCreate
	}
	if resource.Status.OperatingMode != sandboxapi.OperatingModeSuspended {
		return ""
	}
	if !incarnated {
		return modeSuspended
	}
	if stage == sandboxapi.StopStageQuiescing {
		return phaseQuiescing
	}
	return phaseSuspend
}

func (r *sandboxResolver) destination(ctx context.Context, destination *sandboxespb.SandboxDestination, writes bool) (sandboxView, error) {
	if !r.health.Available() {
		return sandboxView{}, status.Error(codes.Unavailable, "Sandbox inventory is not synchronized")
	}
	incarnation, err := r.sandboxes.Incarnation(ctx, destination)
	if err != nil {
		return sandboxView{}, err
	}
	view := routingView(incarnation)
	if writes && (view.phase == phaseQuiescing || view.phase == phaseTearDown) {
		return sandboxView{}, status.Error(codes.FailedPrecondition, "runner or sandbox state refused the request")
	}
	if view.phase != phaseRunning && view.phase != phaseQuiescing {
		return sandboxView{}, status.Error(codes.Unavailable, "destination unavailable; no offline admission")
	}
	if view.incarnation == nil {
		return sandboxView{}, status.Error(codes.Unavailable, "destination unavailable; no offline admission")
	}
	var pod corev1.Pod
	err = r.objects.Get(ctx, client.ObjectKey{Namespace: r.namespace, Name: view.incarnation.Name}, &pod)
	if apierrors.IsNotFound(err) {
		return sandboxView{}, status.Error(codes.Unavailable, "destination unavailable; no offline admission")
	}
	if err != nil {
		return sandboxView{}, fmt.Errorf("read Sandbox runner Pod: %w", err)
	}
	if !podIsReadyForSandbox(&pod, view) {
		return sandboxView{}, status.Error(codes.Unavailable, "destination unavailable; no offline admission")
	}
	return view, nil
}

func podIsReadyForSandbox(pod *corev1.Pod, sandbox sandboxView) bool {
	if pod == nil || pod.UID == "" || string(pod.UID) != sandbox.incarnation.UID || pod.Namespace != sandbox.identity.Namespace ||
		pod.DeletionTimestamp != nil && !pod.DeletionTimestamp.IsZero() || pod.Spec.ServiceAccountName != sandbox.identity.Name || pod.Status.Phase != corev1.PodRunning {
		return false
	}
	ready := false
	for _, condition := range pod.Status.Conditions {
		if condition.Type == corev1.PodReady && condition.Status == corev1.ConditionTrue {
			ready = true
		}
	}
	if !ready {
		return false
	}
	owner := metav1.GetControllerOf(pod)
	if owner == nil {
		return false
	}
	if owner.APIVersion == sandboxapi.GroupVersion.String() && owner.Kind == sandboxapi.SandboxKind {
		return owner.Name == sandbox.identity.Name && string(owner.UID) == sandbox.identity.UID
	}
	return owner.APIVersion == sandboxVMIAPIVer && owner.Kind == sandboxVMIKind &&
		pod.Labels[sandboxapi.SandboxLabel] == sandbox.identity.Name
}
