package supervision

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/cosi"
	systemdops "github.com/agentydragon/ducktape/agentplane/env/runtime/supervision/internal/systemd"
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
	observationType  = "ProcessObservations.agentplane"
	ControllerName   = "process-lifecycle"
)

// Domain identifies the lifecycle barrier responsible for an attempt.
type Domain string

const (
	RuntimeDomain Domain = "environment-runtime"
	HarnessDomain Domain = "harness"
)

type DesiredState struct {
	SystemdScope  SystemdScopeID
	Domain        Domain
	Owner         string
	Generation    uint64
	StartDeadline time.Time
	StopTimeout   time.Duration
}

type observationSpec struct {
	// Revision wakes reconciliation for host-owned start, exit, and stop slots.
	Revision uint64
	// Systemd is an observed fact, not evidence that the process domain is fenced.
	Systemd systemdops.Observation
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
	Owner      string
	Phase      Phase
	Started    bool
	PID        int
	StartError string
	ExitError  string
	StopError  string
	// Systemd is the latest observation of this attempt's VM unit. It is absent
	// for Pod and container-backed processes and is not process-domain proof.
	Systemd systemdops.Observation
}

func (DesiredState) resourceType() resource.Type { return DesiredStateType }
func (value DesiredState) DeepCopy() DesiredState {
	return value
}

func (observationSpec) resourceType() resource.Type     { return observationType }
func (value observationSpec) DeepCopy() observationSpec { return value }

func (Status) resourceType() resource.Type { return StatusType }
func (value Status) DeepCopy() Status      { return value }

const hostOwner = "supervision-host"

type resourceSpec[T any] interface {
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

func writeResource[T resourceSpec[T]](ctx context.Context, writer controller.Writer, namespace resource.Namespace, id string, value T) error {
	return safe.WriterModify(ctx, writer, resourceFor(namespace, id, value), func(current *typed.Resource[T, resourceExtension[T]]) error {
		*current.TypedSpec() = value
		return nil
	})
}

func publishResource[T interface {
	resourceSpec[T]
	comparable
}](ctx context.Context, store state.State, namespace resource.Namespace, id string, value T) error {
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
