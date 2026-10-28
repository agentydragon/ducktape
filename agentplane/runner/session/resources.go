// Package session owns one Runner's durable Session inventory and its COSI lifecycle.
package session

import (
	"context"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	InputType      = "SessionInputs.agentplane"
	DrainInputType = "SessionDrainInputs.agentplane"
	RuntimeType    = "SessionRuntimes.agentplane"
	OperationType  = "NativeOperationResults.agentplane"
	ControllerName = "session-lifecycle"
	HostOwner      = "runner-host"
)

type Phase uint8

const (
	Dormant Phase = iota
	Preparing
	Ready
	Stopping
	Halted
)

type Input struct {
	Harness           runnerpb.Harness
	Demand            uint64
	SetupRequested    bool
	Setup             runnerpb.SetupState
	Pending           int
	ProcessID         string
	ProcessRunning    bool
	Inspections       int
	InspectionRunning bool
	InspectionReady   bool
	DispatchBlocked   bool
}

// DrainInput changes only when inspection work starts or settles. The aggregate
// Runner controller needs this barrier, not every Session dispatch dependency.
type DrainInput struct{ Inspecting bool }

type Runtime struct {
	Phase          Phase
	Attempt        uint64
	Resuming       bool
	ProcessID      string
	Generation     uint64
	SetupAttempt   string
	ProcessStopped bool
	Error          string
}

type operationResult struct {
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
	value, err := safe.ReaderGetByID[*typed.Resource[T, componentExtension[T]]](ctx, reader, id)
	if state.IsNotFoundError(err) {
		return zero, false, nil
	}
	if err != nil {
		return zero, false, err
	}
	return *value.TypedSpec(), true, nil
}

func componentWrite[T componentSpec[T]](ctx context.Context, writer controller.ReaderWriter, id string, value T) (bool, error) {
	old, found, err := componentRead[T](ctx, writer, id)
	if err != nil || found && old == value {
		return false, err
	}
	err = safe.WriterModify(ctx, writer, componentResource(id, value), func(current *typed.Resource[T, componentExtension[T]]) error {
		*current.TypedSpec() = value
		return nil
	})
	return err == nil, err
}

func publish[T componentSpec[T]](ctx context.Context, writer state.State, id string, value T) error {
	return safe.StateModify(ctx, writer, componentResource(id, value), func(current *typed.Resource[T, componentExtension[T]]) error {
		*current.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner(HostOwner))
}

func ReadInput(ctx context.Context, reader controller.Reader, id string) (Input, bool, error) {
	return componentRead[Input](ctx, reader, id)
}

func ReadRuntime(ctx context.Context, reader controller.Reader, id string) (Runtime, bool, error) {
	return componentRead[Runtime](ctx, reader, id)
}

func (Input) resourceType() resource.Type               { return InputType }
func (value Input) DeepCopy() Input                     { return value }
func (Runtime) resourceType() resource.Type             { return RuntimeType }
func (value Runtime) DeepCopy() Runtime                 { return value }
func (operationResult) resourceType() resource.Type     { return OperationType }
func (value operationResult) DeepCopy() operationResult { return value }

func (DrainInput) resourceType() resource.Type { return DrainInputType }
func (value DrainInput) DeepCopy() DrainInput  { return value }
func ReadDrainInput(ctx context.Context, reader controller.Reader, id string) (DrainInput, bool, error) {
	return componentRead[DrainInput](ctx, reader, id)
}
