package view

import (
	"encoding/json"
	"fmt"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	kubernetespb "github.com/agentydragon/ducktape/agentplane/proto/kubernetes"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const PresetLabel = "agentplane.allegedly.works/preset"

func sandboxView(resource *sandboxapi.Sandbox, pod *corev1.Pod) (*environmentpb.EnvironmentDescriptor, error) {
	configuration := &environmentpb.EnvironmentConfiguration{
		Template: resource.Spec.Template, WakeOnSend: resource.Spec.WakeOnSend,
	}
	observedState := &environmentpb.EnvironmentObservedState{
		Phase: sandboxPhase(resource), StopStage: string(resource.Status.StopStage),
		AutosuspendBlockers: make([]*environmentlifecyclepb.AutosuspendBlocker, 0, len(resource.Status.IdleSuspendBlockers)),
	}
	diagnostics := &environmentpb.KubernetesDiagnostics{}
	view := &environmentpb.EnvironmentDescriptor{
		Target:        &identitypb.EnvironmentTarget{Namespace: resource.Namespace, Name: resource.Name, Uid: string(resource.UID)},
		Configuration: configuration, ObservedState: observedState, KubernetesDiagnostics: diagnostics,
	}
	observedState.Readiness = &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_NOT_READY}
	if condition := meta.FindStatusCondition(resource.Status.Conditions, sandboxapi.ConditionReady); condition != nil {
		observedState.Readiness.Reason, observedState.Readiness.Detail = condition.Reason, condition.Message
		if condition.Status == metav1.ConditionTrue {
			observedState.Readiness.State = environmentpb.ReadinessState_READINESS_STATE_READY
		}
	} else {
		observedState.Readiness.Reason = "ReadyConditionUnavailable"
	}
	if resource.Status.Incarnation != nil {
		observedState.RuntimeIncarnationUid = resource.Status.Incarnation.UID
	}
	for _, blocker := range resource.Status.IdleSuspendBlockers {
		observedState.AutosuspendBlockers = append(observedState.AutosuspendBlockers, &environmentlifecyclepb.AutosuspendBlocker{
			Reason: string(blocker.Reason), SessionId: blocker.SessionID,
		})
	}
	if !resource.CreationTimestamp.IsZero() {
		observedState.CreatedAt = timestamppb.New(resource.CreationTimestamp.Time)
	}
	if resource.Spec.Bootstrap != "" {
		configuration.Binding = &environmentpb.SandboxBinding{Bootstrap: resource.Spec.Bootstrap}
	}
	if preset, ok := resource.Labels[PresetLabel]; ok {
		configuration.Preset = &preset
	}
	if resource.Status.OperatingMode != "" {
		switch resource.Status.OperatingMode {
		case sandboxapi.OperatingModeRunning:
			observedState.OperatingMode = environmentpb.OperatingMode_OPERATING_MODE_RUNNING
		case sandboxapi.OperatingModeSuspended:
			observedState.OperatingMode = environmentpb.OperatingMode_OPERATING_MODE_SUSPENDED
		}
	}
	if resource.Status.SuspendedBy != nil {
		suspendedBy := string(*resource.Status.SuspendedBy)
		observedState.SuspendedBy = &suspendedBy
	}
	if resource.Status.IdleSuspendAt != nil {
		observedState.IdleSuspendAt = timestamppb.New(resource.Status.IdleSuspendAt.Time)
	}
	if resource.Spec.IdleSuspendAfter != nil {
		configuration.IdleSuspendAfter = durationpb.New(resource.Spec.IdleSuspendAfter.Duration)
	}
	statusValue, err := jsonValue(resource.Status)
	if err != nil {
		return nil, fmt.Errorf("marshal Sandbox status: %w", err)
	}
	if _, ok := statusValue["conditions"]; !ok {
		statusValue["conditions"] = []any{}
	}
	diagnostics.SandboxStatus, err = structpb.NewStruct(statusValue)
	if err != nil {
		return nil, fmt.Errorf("encode Sandbox status: %w", err)
	}
	if pod != nil && resource.Status.Incarnation != nil && string(pod.UID) == resource.Status.Incarnation.UID {
		diagnostics.Pod, err = sandboxPodView(pod)
		if err != nil {
			return nil, err
		}
	}
	return view, nil
}

// View projects one Sandbox and its Pod only when the Pod is the Sandbox's currently reported
// incarnation.
func View(resource *sandboxapi.Sandbox, pod *corev1.Pod) (*environmentpb.EnvironmentDescriptor, error) {
	if resource == nil {
		return nil, fmt.Errorf("Sandbox is required")
	}
	incarnation := resource.Status.Incarnation
	if pod != nil && (incarnation == nil || pod.Name != incarnation.Name || pod.Namespace != resource.Namespace ||
		string(pod.UID) != incarnation.UID) {
		pod = nil
	}
	return sandboxView(resource, pod)
}

func sandboxPodView(pod *corev1.Pod) (*environmentpb.SandboxPod, error) {
	view := &environmentpb.SandboxPod{
		Name: pod.Name, Namespace: pod.Namespace, Uid: string(pod.UID),
		Deleting: pod.DeletionTimestamp != nil,
	}
	if pod.Spec.NodeName != "" {
		nodeName := pod.Spec.NodeName
		view.NodeName = &nodeName
	}
	for _, owner := range pod.OwnerReferences {
		view.OwnerReferences = append(view.OwnerReferences, &kubernetespb.OwnerReference{
			ApiVersion: owner.APIVersion, Kind: owner.Kind, Name: owner.Name,
			Uid: string(owner.UID), Controller: owner.Controller != nil && *owner.Controller,
		})
	}
	statusValue, err := jsonValue(pod.Status)
	if err != nil {
		return nil, fmt.Errorf("marshal Pod status: %w", err)
	}
	view.Status, err = structpb.NewStruct(statusValue)
	if err != nil {
		return nil, fmt.Errorf("encode Pod status: %w", err)
	}
	return view, nil
}

func sandboxPhase(resource *sandboxapi.Sandbox) environmentpb.SandboxPhase {
	if resource.DeletionTimestamp != nil {
		sealed := resource.Status.StopStage == sandboxapi.StopStageDeleting
		for _, condition := range resource.Status.Conditions {
			if condition.Type == sandboxapi.ConditionQuiesced && condition.Status == metav1.ConditionTrue {
				sealed = true
			}
		}
		if sealed {
			return environmentpb.SandboxPhase_SANDBOX_PHASE_TEARING_DOWN
		}
		return environmentpb.SandboxPhase_SANDBOX_PHASE_QUIESCING
	}
	incarnated := resource.Status.Incarnation != nil
	switch resource.Status.OperatingMode {
	case sandboxapi.OperatingModeRunning:
		if incarnated {
			return environmentpb.SandboxPhase_SANDBOX_PHASE_RUNNING
		}
		return environmentpb.SandboxPhase_SANDBOX_PHASE_CREATING
	case sandboxapi.OperatingModeSuspended:
		if !incarnated || resource.Status.StopStage == sandboxapi.StopStageStopped {
			return environmentpb.SandboxPhase_SANDBOX_PHASE_SUSPENDED
		}
		if resource.Status.StopStage == sandboxapi.StopStageQuiescing {
			return environmentpb.SandboxPhase_SANDBOX_PHASE_QUIESCING
		}
		return environmentpb.SandboxPhase_SANDBOX_PHASE_SUSPENDING
	default:
		return environmentpb.SandboxPhase_SANDBOX_PHASE_UNSPECIFIED
	}
}

func jsonValue(value any) (map[string]any, error) {
	encoded, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var object map[string]any
	if err := json.Unmarshal(encoded, &object); err != nil {
		return nil, err
	}
	if object == nil {
		return map[string]any{}, nil
	}
	return object, nil
}
