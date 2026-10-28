package supervision

import (
	"context"
	"time"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	DesiredStateType = "ProcessDesiredStates.agentplane"
	StatusType       = "ProcessStatuses.agentplane"
	inputType        = "ProcessInputs.agentplane"
	ControllerName   = "process-lifecycle"
)

type DesiredState struct {
	Owner         string
	Generation    uint64
	StartDeadline time.Time
	StopTimeout   time.Duration
}

type inputSpec struct {
	Revision uint64
}

type Phase uint8

const (
	Pending Phase = iota
	Starting
	Running
	Stopping
	Exited
)

type Status struct {
	Owner        string
	Phase        Phase
	Started      bool
	PID          int
	StartError   string
	StartRefusal string
	ExitError    string
	StopError    string
}

// LaunchRefusal distinguishes a launch that was rejected by policy or
// capacity from a launch that failed after it was accepted.
type LaunchRefusal struct {
	Reason string
	Cause  error
}

func (e *LaunchRefusal) Error() string {
	if e.Cause != nil {
		return e.Cause.Error()
	}
	return e.Reason
}

func (e *LaunchRefusal) Unwrap() error { return e.Cause }

func (DesiredState) resourceType() resource.Type { return DesiredStateType }
func (value DesiredState) DeepCopy() DesiredState {
	return value
}

func (inputSpec) resourceType() resource.Type { return inputType }
func (value inputSpec) DeepCopy() inputSpec   { return value }

func (Status) resourceType() resource.Type { return StatusType }
func (value Status) DeepCopy() Status      { return value }

const hostOwner = "supervision-host"

type resourceSpec[T any] interface {
	comparable
	typed.DeepCopyable[T]
	resourceType() resource.Type
}

type resourceExtension[T resourceSpec[T]] struct{}

func (resourceExtension[T]) ResourceDefinition() spec.ResourceDefinitionSpec {
	var value T
	return spec.ResourceDefinitionSpec{Type: value.resourceType()}
}

func resourceFor[T resourceSpec[T]](namespace resource.Namespace, id string, value T) *typed.Resource[T, resourceExtension[T]] {
	return typed.NewResource[T, resourceExtension[T]](resource.NewMetadata(namespace, value.resourceType(), id, resource.VersionUndefined), value)
}

func readResource[T resourceSpec[T]](ctx context.Context, reader controller.Reader, namespace resource.Namespace, id string) (T, bool, error) {
	var zero T
	r, err := safe.ReaderGet[*typed.Resource[T, resourceExtension[T]]](ctx, reader, resourceFor(namespace, id, zero).Metadata())
	if state.IsNotFoundError(err) {
		return zero, false, nil
	}
	if err != nil {
		return zero, false, err
	}
	return *r.TypedSpec(), true, nil
}

func writeResource[T resourceSpec[T]](ctx context.Context, writer controller.ReaderWriter, namespace resource.Namespace, id string, value T) (bool, error) {
	old, found, err := readResource[T](ctx, writer, namespace, id)
	if err != nil || found && old == value {
		return false, err
	}
	err = safe.WriterModify(ctx, writer, resourceFor(namespace, id, value), func(r *typed.Resource[T, resourceExtension[T]]) error {
		*r.TypedSpec() = value
		return nil
	})
	return err == nil, err
}

func publishResource[T resourceSpec[T]](ctx context.Context, store state.State, namespace resource.Namespace, id string, value T) error {
	return safe.StateModify(ctx, store, resourceFor(namespace, id, value), func(r *typed.Resource[T, resourceExtension[T]]) error {
		*r.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner(hostOwner))
}

// ReadDesiredState reads an attempt from the caller's existing COSI runtime.
func ReadDesiredState(ctx context.Context, reader controller.Reader, namespace resource.Namespace, id string) (DesiredState, bool, error) {
	return readResource[DesiredState](ctx, reader, namespace, id)
}

// ReadStatus reads the process controller's observations for one launch.
func ReadStatus(ctx context.Context, reader controller.Reader, namespace resource.Namespace, id string) (Status, bool, error) {
	return readResource[Status](ctx, reader, namespace, id)
}
