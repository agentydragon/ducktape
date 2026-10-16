package gateway

import (
	"context"
	"fmt"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/dynamic"
	coreclient "k8s.io/client-go/kubernetes/typed/core/v1"
)

const (
	sandboxGroup      = "agentplane.allegedly.works"
	sandboxVersion    = "v1alpha1"
	sandboxResource   = "sandboxes"
	sandboxAPIVersion = sandboxGroup + "/" + sandboxVersion
	sandboxKind       = "Sandbox"
	sandboxLabel      = sandboxGroup + "/sandbox"
	sandboxRunning    = "Running"
	sandboxReady      = "Ready"
	sandboxQuiesced   = "Quiesced"
	sandboxVMIAPIVer  = "kubevirt.io/v1"
	sandboxVMIKind    = "VirtualMachineInstance"
	phaseRunning      = "Running"
	phaseQuiescing    = "Quiescing"
	phaseDeleting     = "Deleting"
	modeRunning       = "Running"
	modeSuspended     = "Suspended"
	phaseCreate       = "Creating"
	phaseSuspend      = "Suspending"
	phaseTearDown     = "TearingDown"
)

var sandboxesGVR = schema.GroupVersionResource{Group: sandboxGroup, Version: sandboxVersion, Resource: sandboxResource}

// SandboxIdentity is the exact Sandbox incarnation named by an authenticated runner or proxy
// request. Namespace/name alone are not sufficient because Kubernetes permits name reuse.
type SandboxIdentity struct {
	Namespace string
	Name      string
	UID       string
}

type sandboxDocument struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              struct {
		Running        bool  `json:"running"`
		WakeGeneration int64 `json:"wakeGeneration,omitempty"`
	} `json:"spec"`
	Status struct {
		OperatingMode string `json:"operatingMode,omitempty"`
		Incarnation   *struct {
			Name string `json:"name"`
			UID  string `json:"uid"`
		} `json:"incarnation,omitempty"`
		SuspendedBy               *string            `json:"suspendedBy,omitempty"`
		SuspendedAtWakeGeneration *int64             `json:"suspendedAtWakeGeneration,omitempty"`
		QuiescedIncarnation       *string            `json:"quiescedIncarnation,omitempty"`
		Conditions                []metav1.Condition `json:"conditions,omitempty"`
	} `json:"status,omitempty"`
}

type sandboxView struct {
	resource    sandboxDocument
	identity    SandboxIdentity
	phase       string
	incarnation *struct {
		Name string
		UID  string
	}
}

type sandboxReader struct {
	namespace string
	objects   dynamic.Interface
	core      coreclient.CoreV1Interface
}

func newSandboxReader(namespace string, objects dynamic.Interface, core coreclient.CoreV1Interface) (*sandboxReader, error) {
	if namespace == "" || objects == nil || core == nil {
		return nil, fmt.Errorf("Sandbox namespace and Kubernetes clients are required")
	}
	return &sandboxReader{namespace: namespace, objects: objects, core: core}, nil
}

func (r *sandboxReader) get(ctx context.Context, name string) (sandboxView, error) {
	object, err := r.objects.Resource(sandboxesGVR).Namespace(r.namespace).Get(ctx, name, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return sandboxView{}, status.Error(codes.NotFound, "sandbox not found")
	}
	if err != nil {
		return sandboxView{}, fmt.Errorf("read Sandbox: %w", err)
	}
	var resource sandboxDocument
	if err := runtime.DefaultUnstructuredConverter.FromUnstructured(object.Object, &resource); err != nil {
		return sandboxView{}, fmt.Errorf("decode Sandbox: %w", err)
	}
	if resource.UID == "" || resource.Name == "" || resource.Namespace != r.namespace {
		return sandboxView{}, status.Error(codes.Unavailable, "Sandbox identity is incomplete")
	}
	view := sandboxView{
		resource: resource,
		identity: SandboxIdentity{Namespace: resource.Namespace, Name: resource.Name, UID: string(resource.UID)},
		phase:    sandboxPhase(&resource),
	}
	if resource.Status.Incarnation != nil {
		view.incarnation = &struct {
			Name string
			UID  string
		}{Name: resource.Status.Incarnation.Name, UID: resource.Status.Incarnation.UID}
	}
	return view, nil
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

func sandboxPhase(resource *sandboxDocument) string {
	runningReason := ""
	sealed := false
	for _, condition := range resource.Status.Conditions {
		if condition.Type == sandboxRunning {
			runningReason = condition.Reason
		}
		if condition.Type == sandboxQuiesced && condition.Status == metav1.ConditionTrue {
			sealed = true
		}
	}
	incarnated := resource.Status.Incarnation != nil
	if !resource.DeletionTimestamp.IsZero() {
		if sealed || runningReason == phaseDeleting {
			return phaseTearDown
		}
		return phaseQuiescing
	}
	if resource.Status.OperatingMode == "" {
		return ""
	}
	if resource.Status.OperatingMode == modeRunning {
		if incarnated {
			return phaseRunning
		}
		return phaseCreate
	}
	if resource.Status.OperatingMode != modeSuspended {
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
		!pod.DeletionTimestamp.IsZero() || pod.Spec.ServiceAccountName != sandbox.identity.Name || pod.Status.Phase != corev1.PodRunning {
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
	controllers := make([]metav1.OwnerReference, 0, 1)
	for _, owner := range pod.OwnerReferences {
		if owner.Controller != nil && *owner.Controller {
			controllers = append(controllers, owner)
		}
	}
	if len(controllers) != 1 {
		return false
	}
	owner := controllers[0]
	if owner.APIVersion == sandboxAPIVersion && owner.Kind == sandboxKind {
		return owner.Name == sandbox.identity.Name && string(owner.UID) == sandbox.identity.UID
	}
	return owner.APIVersion == sandboxVMIAPIVer && owner.Kind == sandboxVMIKind &&
		pod.Labels[sandboxLabel] == sandbox.identity.Name
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
	if string(pod.UID) != principal.PodUID || !pod.DeletionTimestamp.IsZero() {
		return SandboxIdentity{}, "", status.Error(codes.Unauthenticated, "invalid workload bearer")
	}
	controllers := make([]metav1.OwnerReference, 0, 1)
	for _, owner := range pod.OwnerReferences {
		if owner.Controller != nil && *owner.Controller {
			controllers = append(controllers, owner)
		}
	}
	if len(controllers) != 1 {
		return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
	}
	owner := controllers[0]
	sandboxName := owner.Name
	if owner.APIVersion == sandboxVMIAPIVer && owner.Kind == sandboxVMIKind {
		sandboxName = pod.Labels[sandboxLabel]
		if sandboxName == "" {
			return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
		}
	} else if owner.APIVersion != sandboxAPIVersion || owner.Kind != sandboxKind {
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
	if owner.APIVersion == sandboxAPIVersion && owner.UID != types.UID(view.identity.UID) {
		return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
	}
	if owner.APIVersion == sandboxVMIAPIVer && (view.incarnation == nil || view.incarnation.UID != string(pod.UID)) {
		return SandboxIdentity{}, "", status.Error(codes.PermissionDenied, "not a Sandbox Pod")
	}
	return view.identity, string(pod.UID), nil
}
