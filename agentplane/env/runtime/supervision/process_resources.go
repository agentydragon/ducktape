package supervision

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	DesiredStateType = "ProcessDesiredStates.agentplane"
	StatusType       = "ProcessStatuses.agentplane"
	inputType        = "ProcessInputs.agentplane"
	ControllerName   = "process-lifecycle"
)

// Domain identifies the lifecycle barrier responsible for an attempt.
type Domain string

const (
	RuntimeDomain Domain = "environment-runtime"
	HarnessDomain Domain = "harness-host"
)

type DesiredState struct {
	Domain        Domain
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
	// Systemd is the latest observation of this attempt's VM unit. It is absent
	// for Pod and container-backed processes and is not process-domain proof.
	Systemd SystemdObservation
}

// SystemdObservation is a manager observation for one process attempt. Revision
// orders callbacks from concurrent backend workers; it is not durable state.
type SystemdObservation struct {
	Revision             uint64
	Unit                 string
	StartState           SystemdStartState
	StartError           string
	ExpectedInvocationID string
	CapturedControlGroup string
	Snapshot             SystemdUnitSnapshot
	Error                string
	ExitState            SystemdExitState
	ExitCode             int
	ExitError            string
}

type SystemdExitState uint8

const (
	SystemdExitPending SystemdExitState = iota
	SystemdExitObserved
	SystemdExitUnknown
)

func (state SystemdExitState) String() string {
	switch state {
	case SystemdExitPending:
		return "pending"
	case SystemdExitObserved:
		return "observed"
	case SystemdExitUnknown:
		return "unknown"
	default:
		return "invalid"
	}
}

// SystemdStartState records the reply to a single start request. A lost reply
// stays unconfirmed even if cleanup later succeeds; it is not permission to retry.
type SystemdStartState uint8

const (
	SystemdPreparing SystemdStartState = iota
	SystemdStartSubmitted
	SystemdStartAccepted
	SystemdStartUnconfirmed
)

func (state SystemdStartState) String() string {
	switch state {
	case SystemdPreparing:
		return "preparing"
	case SystemdStartSubmitted:
		return "submitted"
	case SystemdStartAccepted:
		return "accepted"
	case SystemdStartUnconfirmed:
		return "unconfirmed"
	default:
		return "invalid"
	}
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
	return cosi.Read(ctx, reader, resourceFor(namespace, id, zero))
}

func writeResource[T resourceSpec[T]](ctx context.Context, writer controller.ReaderWriter, namespace resource.Namespace, id string, value T) (bool, error) {
	return cosi.Write(ctx, writer, resourceFor(namespace, id, value), value)
}

func publishResource[T resourceSpec[T]](ctx context.Context, store state.State, namespace resource.Namespace, id string, value T) error {
	return cosi.Publish(ctx, store, resourceFor(namespace, id, value), value, hostOwner)
}

// ReadDesiredState reads an attempt from the caller's existing COSI runtime.
func ReadDesiredState(ctx context.Context, reader controller.Reader, namespace resource.Namespace, id string) (DesiredState, bool, error) {
	return readResource[DesiredState](ctx, reader, namespace, id)
}

// ReadStatus reads the process controller's observations for one launch.
func ReadStatus(ctx context.Context, reader controller.Reader, namespace resource.Namespace, id string) (Status, bool, error) {
	return readResource[Status](ctx, reader, namespace, id)
}
