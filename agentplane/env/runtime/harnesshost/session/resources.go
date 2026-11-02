// Package session owns the Harness Host's durable Session inventory and its COSI lifecycle.
package session

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	ReconcileSnapshotType = "SessionReconcileSnapshots.agentplane"
	InspectionStatusType = "SessionInspectionStatuses.agentplane"
	LifecycleStatusType  = "SessionLifecycleStatuses.agentplane"
	OperationStateType   = "SessionOperationStates.agentplane"
	ControllerName       = "session-lifecycle"
	HostOwner            = lifecycle.HostOwner
)

type Phase uint8

const (
	Dormant Phase = iota
	Preparing
	Ready
	Stopping
	Halted
)

type ReconcileSnapshot struct {
	Harness           sessionpb.Harness
	Demand            uint64
	SetupRequested    bool
	Setup             sessionpb.SetupState
	Pending           int
	ProcessID         string
	ProcessRunning    bool
	Inspections       int
	InspectionRunning bool
	InspectionReady   bool
	DispatchBlocked   bool
}

// InspectionStatus changes only when inspection work starts or settles. The Harness Host
// lifecycle controller needs this barrier, not every Session dispatch dependency.
type InspectionStatus struct{ Inspecting bool }

type LifecycleStatus struct {
	Phase            Phase
	ObservedDemand   uint64
	Resuming         bool
	ProcessID        string
	HostGeneration   uint64
	SetupOperationID string
	ProcessStopped   bool
	Error            string
}

type operationState struct {
	Session    string
	Controller string
	Done       bool
}

type componentSpec[T any] interface {
	comparable
	typed.DeepCopyable[T]
	resourceType() resource.Type
}

type componentExtension[T componentSpec[T]] struct{}

func (componentExtension[T]) ResourceDefinition() spec.ResourceDefinitionSpec {
	var value T
	return spec.ResourceDefinitionSpec{Type: value.resourceType(), DefaultNamespace: lifecycle.Namespace}
}

func componentResource[T componentSpec[T]](id string, value T) *typed.Resource[T, componentExtension[T]] {
	return typed.NewResource[T, componentExtension[T]](
		resource.NewMetadata(lifecycle.Namespace, value.resourceType(), id, resource.VersionUndefined), value,
	)
}

func componentRead[T componentSpec[T]](ctx context.Context, reader controller.Reader, id string) (T, bool, error) {
	var zero T
	return cosi.Read(ctx, reader, componentResource(id, zero))
}

func componentWrite[T componentSpec[T]](ctx context.Context, writer controller.ReaderWriter, id string, value T) (bool, error) {
	return cosi.Write(ctx, writer, componentResource(id, value), value)
}

func publish[T componentSpec[T]](ctx context.Context, writer state.State, id string, value T) error {
	return cosi.Publish(ctx, writer, componentResource(id, value), value, HostOwner)
}

func ReadReconcileSnapshot(ctx context.Context, reader controller.Reader, id string) (ReconcileSnapshot, bool, error) {
	return componentRead[ReconcileSnapshot](ctx, reader, id)
}

func ReadLifecycleStatus(ctx context.Context, reader controller.Reader, id string) (LifecycleStatus, bool, error) {
	return componentRead[LifecycleStatus](ctx, reader, id)
}

func (ReconcileSnapshot) resourceType() resource.Type       { return ReconcileSnapshotType }
func (value ReconcileSnapshot) DeepCopy() ReconcileSnapshot { return value }
func (LifecycleStatus) resourceType() resource.Type         { return LifecycleStatusType }
func (value LifecycleStatus) DeepCopy() LifecycleStatus     { return value }
func (operationState) resourceType() resource.Type          { return OperationStateType }
func (value operationState) DeepCopy() operationState       { return value }

func (InspectionStatus) resourceType() resource.Type      { return InspectionStatusType }
func (value InspectionStatus) DeepCopy() InspectionStatus { return value }
func ReadInspectionStatus(ctx context.Context, reader controller.Reader, id string) (InspectionStatus, bool, error) {
	return componentRead[InspectionStatus](ctx, reader, id)
}
