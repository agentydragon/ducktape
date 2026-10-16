package gateway

import (
	"context"
	"fmt"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/auth"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	coreclient "k8s.io/client-go/kubernetes/typed/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	sandboxVMIAPIVer = "kubevirt.io/v1"
	sandboxVMIKind   = "VirtualMachineInstance"
	phaseRunning     = "Running"
	phaseQuiescing   = "Quiescing"
	phaseDeleting    = "Deleting"
	modeSuspended    = "Suspended"
	phaseCreate      = "Creating"
	phaseSuspend     = "Suspending"
	phaseTearDown    = "TearingDown"
)

// SandboxIdentity is the exact Sandbox incarnation named by an authenticated runner or proxy
// request. Namespace/name alone are not sufficient because Kubernetes permits name reuse.
type SandboxIdentity struct {
	Namespace string
	Name      string
	UID       string
}

type sandboxView struct {
	identity    SandboxIdentity
	phase       string
	incarnation *sandboxapi.Incarnation
}

type sandboxReader struct {
	namespace string
	objects   client.Reader
	core      coreclient.CoreV1Interface
}

func newSandboxReader(namespace string, objects client.Reader, core coreclient.CoreV1Interface) (*sandboxReader, error) {
	if namespace == "" || objects == nil || core == nil {
		return nil, fmt.Errorf("Sandbox namespace and Kubernetes clients are required")
	}
	return &sandboxReader{namespace: namespace, objects: objects, core: core}, nil
}

func (r *sandboxReader) get(ctx context.Context, name string) (sandboxView, error) {
	var resource sandboxapi.Sandbox
	err := r.objects.Get(ctx, types.NamespacedName{Namespace: r.namespace, Name: name}, &resource)
	if apierrors.IsNotFound(err) {
		return sandboxView{}, status.Error(codes.NotFound, "sandbox not found")
	}
	if err != nil {
		return sandboxView{}, fmt.Errorf("read Sandbox: %w", err)
	}
	if resource.UID == "" || resource.Name == "" || resource.Namespace != r.namespace {
		return sandboxView{}, status.Error(codes.Unavailable, "Sandbox identity is incomplete")
	}
	return sandboxView{
		identity: SandboxIdentity{Namespace: resource.Namespace, Name: resource.Name, UID: string(resource.UID)},
		phase:    sandboxPhase(&resource), incarnation: resource.Status.Incarnation,
	}, nil
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
	runningReason := ""
	sealed := false
	if running := meta.FindStatusCondition(resource.Status.Conditions, sandboxapi.ConditionRunning); running != nil {
		runningReason = running.Reason
	}
	if quiesced := meta.FindStatusCondition(resource.Status.Conditions, sandboxapi.ConditionQuiesced); quiesced != nil && quiesced.Status == metav1.ConditionTrue {
		sealed = true
	}
	incarnated := resource.Status.Incarnation != nil
	if resource.DeletionTimestamp != nil && !resource.DeletionTimestamp.IsZero() {
		if sealed || runningReason == phaseDeleting {
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
	if runningReason == phaseQuiescing {
		return phaseQuiescing
	}
	return phaseSuspend
}

func (r *sandboxReader) destination(ctx context.Context, destination *sandboxespb.SandboxDestination, writes bool) (sandboxView, error) {
	if destination == nil || destination.Sandbox == "" || destination.SandboxUid == "" || destination.Owner == nil ||
		destination.Owner.Namespace == "" || destination.Owner.Name == "" {
		return sandboxView{}, status.Error(codes.InvalidArgument, "Sandbox name, UID, and owner are required")
	}
	if destination.Owner.Namespace != r.namespace || destination.Owner.Name != destination.Sandbox {
		return sandboxView{}, status.Error(codes.NotFound, "sandbox incarnation not found")
	}
	view, err := r.get(ctx, destination.Sandbox)
	if err != nil {
		return sandboxView{}, err
	}
	if view.identity.UID != destination.SandboxUid {
		return sandboxView{}, status.Error(codes.NotFound, "sandbox incarnation not found")
	}
	if writes && (view.phase == phaseQuiescing || view.phase == phaseTearDown) {
		return sandboxView{}, status.Error(codes.FailedPrecondition, "runner or sandbox state refused the request")
	}
	if view.phase != phaseRunning && view.phase != phaseQuiescing {
		return sandboxView{}, status.Error(codes.Unavailable, "destination unavailable; no offline admission")
	}
	if view.incarnation == nil {
		return sandboxView{}, status.Error(codes.Unavailable, "destination unavailable; no offline admission")
	}
	pod, err := r.core.Pods(r.namespace).Get(ctx, view.incarnation.Name, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return sandboxView{}, status.Error(codes.Unavailable, "destination unavailable; no offline admission")
	}
	if err != nil {
		return sandboxView{}, fmt.Errorf("read Sandbox runner Pod: %w", err)
	}
	if !podIsReadyForSandbox(pod, view) {
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

func (r *sandboxReader) runnerPeer(ctx context.Context, principal auth.Principal) (SandboxIdentity, string, error) {
	if principal.Account.Namespace != r.namespace || principal.PodName == "" || principal.PodUID == "" {
		return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
	}
	pod, err := r.core.Pods(r.namespace).Get(ctx, principal.PodName, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return SandboxIdentity{}, "", status.Error(codes.Unauthenticated, "invalid workload bearer")
	}
	if err != nil {
		return SandboxIdentity{}, "", fmt.Errorf("read runner Pod: %w", err)
	}
	if string(pod.UID) != principal.PodUID || pod.DeletionTimestamp != nil && !pod.DeletionTimestamp.IsZero() {
		return SandboxIdentity{}, "", status.Error(codes.Unauthenticated, "invalid workload bearer")
	}
	owner := metav1.GetControllerOf(pod)
	if owner == nil {
		return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
	}
	sandboxName := owner.Name
	if owner.APIVersion == sandboxVMIAPIVer && owner.Kind == sandboxVMIKind {
		sandboxName = pod.Labels[sandboxapi.SandboxLabel]
		if sandboxName == "" {
			return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
		}
	} else if owner.APIVersion != sandboxapi.GroupVersion.String() || owner.Kind != sandboxapi.SandboxKind {
		return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
	}
	view, err := r.get(ctx, sandboxName)
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
		}
		return SandboxIdentity{}, "", err
	}
	if principal.Account.Name != view.identity.Name {
		return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
	}
	if owner.APIVersion == sandboxapi.GroupVersion.String() && owner.UID != types.UID(view.identity.UID) {
		return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
	}
	if owner.APIVersion == sandboxVMIAPIVer && (view.incarnation == nil || view.incarnation.UID != string(pod.UID)) {
		return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
	}
	return view.identity, string(pod.UID), nil
}
