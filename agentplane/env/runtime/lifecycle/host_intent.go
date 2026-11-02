package lifecycle

import (
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
)

const (
	HostIntentType       = "HostIntents.agentplane"
	HostIntentStatusType = "HostIntentStatuses.agentplane"
)

// HostIntent is the immutable input to Host admission; native handles remain outside COSI.
type HostIntent struct {
	RuntimeBootID   string
	HostBootID      string
	Generation      uint64
	SandboxUID      string
	IncarnationUID  string
	Mode            runtimepb.LifecycleIntent_Mode
	SealUID         string
	ExecutionFenced bool
}

func (v HostIntent) DeepCopy() HostIntent { return v }
func HostIntentFromProto(v *runtimepb.LifecycleIntent) HostIntent {
	return HostIntent{RuntimeBootID: v.Binding.RuntimeBootId, HostBootID: v.Binding.HostBootId, Generation: v.Binding.Generation, SandboxUID: v.SandboxUid, IncarnationUID: v.IncarnationUid, Mode: v.Mode, SealUID: v.SealUid, ExecutionFenced: v.ExecutionFenced}
}

func (v HostIntent) Proto() *runtimepb.LifecycleIntent {
	return &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: v.RuntimeBootID, HostBootId: v.HostBootID, Generation: v.Generation}, SandboxUid: v.SandboxUID, IncarnationUid: v.IncarnationUID, Mode: v.Mode, SealUid: v.SealUID, ExecutionFenced: v.ExecutionFenced}
}

type HostIntentStatus struct {
	Intent  HostIntent
	Applied bool
	Error   string
}

func (v HostIntentStatus) DeepCopy() HostIntentStatus { return v }

type hostIntentExtension struct{}

func (hostIntentExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: HostIntentType, DefaultNamespace: Namespace}
}

func HostIntentResource(v HostIntent) *typed.Resource[HostIntent, hostIntentExtension] {
	return typed.NewResource[HostIntent, hostIntentExtension](resource.NewMetadata(Namespace, HostIntentType, "host", resource.VersionUndefined), v)
}

type hostIntentStatusExtension struct{}

func (hostIntentStatusExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: HostIntentStatusType, DefaultNamespace: Namespace}
}

func HostIntentStatusResource(v HostIntentStatus) *typed.Resource[HostIntentStatus, hostIntentStatusExtension] {
	return typed.NewResource[HostIntentStatus, hostIntentStatusExtension](resource.NewMetadata(Namespace, HostIntentStatusType, "host", resource.VersionUndefined), v)
}
