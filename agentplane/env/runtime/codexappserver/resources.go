package codexappserver

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	LifecycleSignalType = "CodexAppServerLifecycleSignals.agentplane"
	StatusType          = "CodexAppServerStatuses.agentplane"
	ControllerName      = "codex-app-server-lifecycle"
	ResourceID          = "codex-app-server"
	HostOwner           = "codex-app-server-host"
)

type LifecycleSignal struct {
	OperationRevision uint64
	Demand            uint64
	ExitSequence      uint64
	ExitProcessID     string
	ExitError         string
}

type Phase uint8

const (
	Stopped Phase = iota
	Initializing
	Ready
	Draining
)

type Status struct {
	Phase                   Phase
	EnvironmentGeneration   uint64
	ObservedDemand          uint64
	ExitSequence            uint64
	ExecServerReadyDeadline time.Time
	ProcessID               string
	Error                   string
	StopError               string
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
	return typed.NewResource[T, componentExtension[T]](resource.NewMetadata(lifecycle.Namespace, value.resourceType(), id, resource.VersionUndefined), value)
}

func componentRead[T componentSpec[T]](ctx context.Context, reader controller.Reader, id string) (T, bool, error) {
	var zero T
	return cosi.Read(ctx, reader, componentResource(id, zero))
}

func componentWrite[T componentSpec[T]](ctx context.Context, writer controller.ReaderWriter, id string, value T) (bool, error) {
	return cosi.Write(ctx, writer, componentResource(id, value), value)
}

func publish[T componentSpec[T]](ctx context.Context, store state.State, id string, value T) error {
	return cosi.Publish(ctx, store, componentResource(id, value), value, HostOwner)
}

func ReadLifecycleSignal(ctx context.Context, reader controller.Reader) (LifecycleSignal, bool, error) {
	return componentRead[LifecycleSignal](ctx, reader, ResourceID)
}

func ReadStatus(ctx context.Context, reader controller.Reader) (Status, bool, error) {
	return componentRead[Status](ctx, reader, ResourceID)
}

func (LifecycleSignal) resourceType() resource.Type     { return LifecycleSignalType }
func (value LifecycleSignal) DeepCopy() LifecycleSignal { return value }
func (Status) resourceType() resource.Type              { return StatusType }
func (value Status) DeepCopy() Status                   { return value }

// Mutations preserve independent operation wakeups across demand resets and exit updates.
func (c *Host) updateLifecycleSignal(ctx context.Context, update func(*LifecycleSignal)) error {
	_, err := safe.StateUpdateWithConflicts(ctx, c.deps.State, componentResource(ResourceID, LifecycleSignal{}).Metadata(), func(current *typed.Resource[LifecycleSignal, componentExtension[LifecycleSignal]]) error {
		update(current.TypedSpec())
		return nil
	}, state.WithUpdateOwner(HostOwner))
	return err
}
