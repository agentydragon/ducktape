package view

import (
	"encoding/json"
	"fmt"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const PresetLabel = "agentplane.allegedly.works/preset"

// View builds the shared Sandbox portion of an EnvironmentDescriptor. The Pod is included only
// when it is the currently reported incarnation, so stale same-name Pods never look current.
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

func sandboxView(resource *sandboxapi.Sandbox, pod *corev1.Pod) (*environmentpb.EnvironmentDescriptor, error) {
	readiness := &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_NOT_READY}
	condition := meta.FindStatusCondition(resource.Status.Conditions, sandboxapi.ConditionReady)
	if condition == nil {
		readiness.Reason = "ReadyConditionUnavailable"
	} else {
		readiness.Detail = condition.Message
		readiness.Reason = condition.Reason
		if condition.Status == metav1.ConditionTrue {
			readiness.State = environmentpb.ReadinessState_READINESS_STATE_READY
		}
	}
	mode := environmentpb.OperatingMode_OPERATING_MODE_UNSPECIFIED
	switch resource.Status.OperatingMode {
	case sandboxapi.OperatingModeRunning:
		mode = environmentpb.OperatingMode_OPERATING_MODE_RUNNING
	case sandboxapi.OperatingModeSuspended:
		mode = environmentpb.OperatingMode_OPERATING_MODE_SUSPENDED
	}
	view := &environmentpb.EnvironmentDescriptor{
		Target:              &environmentpb.TargetHandle{Namespace: resource.Namespace, Name: resource.Name, Uid: string(resource.UID)},
		Readiness:           readiness,
		Template:            resource.Spec.Template,
		OperatingMode:       mode,
		StopStage:           string(resource.Status.StopStage),
		WakeOnSend:          resource.Spec.WakeOnSend,
		Phase:               sandboxPhase(resource),
		AutosuspendBlockers: make([]*sandboxespb.AutosuspendBlocker, 0, len(resource.Status.IdleSuspendBlockers)),
	}
	for _, blocker := range resource.Status.IdleSuspendBlockers {
		view.AutosuspendBlockers = append(view.AutosuspendBlockers, &sandboxespb.AutosuspendBlocker{
			Reason: string(blocker.Reason), SessionId: blocker.SessionID,
		})
	}
	if !resource.CreationTimestamp.IsZero() {
		view.CreatedAt = timestamppb.New(resource.CreationTimestamp.Time)
	}
	if resource.Spec.Bootstrap != "" {
		view.Binding = &sandboxespb.SandboxBinding{Bootstrap: resource.Spec.Bootstrap}
	}
	if preset, ok := resource.Labels[PresetLabel]; ok {
		view.Preset = &preset
	}
	if resource.Status.SuspendedBy != nil {
		suspendedBy := string(*resource.Status.SuspendedBy)
		view.SuspendedBy = &suspendedBy
	}
	if resource.Status.IdleSuspendAt != nil {
		view.IdleSuspendAt = timestamppb.New(resource.Status.IdleSuspendAt.Time)
	}
	if resource.Spec.IdleSuspendAfter != nil {
		view.IdleSuspendAfter = durationpb.New(resource.Spec.IdleSuspendAfter.Duration)
	}
	if resource.Status.Incarnation != nil {
		view.RuntimeIncarnationUid = resource.Status.Incarnation.UID
	}
	statusValue, err := jsonValue(resource.Status)
	if err != nil {
		return nil, fmt.Errorf("marshal Sandbox status: %w", err)
	}
	if _, ok := statusValue["conditions"]; !ok {
		statusValue["conditions"] = []any{}
	}
	view.Status, err = structpb.NewStruct(statusValue)
	if err != nil {
		return nil, fmt.Errorf("encode Sandbox status: %w", err)
	}
	if pod != nil {
		view.Pod, err = sandboxPodView(pod)
		if err != nil {
			return nil, err
		}
	}
	return view, nil
}

func sandboxPodView(pod *corev1.Pod) (*sandboxespb.SandboxPod, error) {
	view := &sandboxespb.SandboxPod{
		Name: pod.Name, Namespace: pod.Namespace, Uid: string(pod.UID),
		Deleting: pod.DeletionTimestamp != nil,
	}
	if pod.Spec.NodeName != "" {
		nodeName := pod.Spec.NodeName
		view.NodeName = &nodeName
	}
	for _, owner := range pod.OwnerReferences {
		view.OwnerReferences = append(view.OwnerReferences, &sandboxespb.OwnerReference{
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

func sandboxPhase(resource *sandboxapi.Sandbox) sandboxespb.SandboxPhase {
	if resource.DeletionTimestamp != nil {
		sealed := resource.Status.StopStage == sandboxapi.StopStageDeleting
		for _, condition := range resource.Status.Conditions {
			if condition.Type == sandboxapi.ConditionQuiesced && condition.Status == metav1.ConditionTrue {
				sealed = true
			}
		}
		if sealed {
			return sandboxespb.SandboxPhase_SANDBOX_PHASE_TEARING_DOWN
		}
		return sandboxespb.SandboxPhase_SANDBOX_PHASE_QUIESCING
	}
	incarnated := resource.Status.Incarnation != nil
	switch resource.Status.OperatingMode {
	case sandboxapi.OperatingModeRunning:
		if incarnated {
			return sandboxespb.SandboxPhase_SANDBOX_PHASE_RUNNING
		}
		return sandboxespb.SandboxPhase_SANDBOX_PHASE_CREATING
	case sandboxapi.OperatingModeSuspended:
		if !incarnated {
			return sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDED
		}
		if resource.Status.StopStage == sandboxapi.StopStageQuiescing {
			return sandboxespb.SandboxPhase_SANDBOX_PHASE_QUIESCING
		}
		return sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDING
	default:
		return sandboxespb.SandboxPhase_SANDBOX_PHASE_UNSPECIFIED
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
