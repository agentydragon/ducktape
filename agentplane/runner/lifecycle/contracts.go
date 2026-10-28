// Package lifecycle defines only the Runner-wide desired and aggregate status resources shared by
// independently owned lifecycle controllers.
package lifecycle

import (
	"context"
	"fmt"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	Namespace           = "runner"
	DesiredStateType    = "RunnerDesiredStates.agentplane"
	AggregateStatusType = "RunnerRuntimes.agentplane"
	ResourceID          = "runner"
	HostOwner           = "runner-host"
)

type Mode uint8

const (
	Running Mode = iota
	Quiescing
	Closing
)

type Stage uint8

const (
	StageRunning Stage = iota
	Draining
	Halting
	Stopping
	Fencing
	Sealing
	Flushing
	Quiesced
	Closed
)

type Desired struct {
	Mode       Mode
	SealUID    string
	Generation uint64
}

func (value Desired) DeepCopy() Desired { return value }

type Status struct {
	Generation uint64
	Stage      Stage
	Error      string
}

func (value Status) DeepCopy() Status { return value }

// AdmissionGuard runs effect while the Runner admission barrier is held, but only while expected
// remains the current Running desired state.
type AdmissionGuard func(ctx context.Context, expected Desired, effect func(context.Context) error) error

type desiredExtension struct{}

func (desiredExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: DesiredStateType, DefaultNamespace: Namespace}
}

type statusExtension struct{}

func (statusExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: AggregateStatusType, DefaultNamespace: Namespace}
}

func ReadDesired(ctx context.Context, reader controller.Reader) (Desired, bool, error) {
	resource, err := safe.ReaderGetByID[*typed.Resource[Desired, desiredExtension]](ctx, reader, ResourceID)
	if state.IsNotFoundError(err) {
		return Desired{}, false, nil
	}
	if err != nil {
		return Desired{}, false, err
	}
	return *resource.TypedSpec(), true, nil
}

func ReadStatus(ctx context.Context, reader controller.Reader) (Status, bool, error) {
	resource, err := safe.ReaderGetByID[*typed.Resource[Status, statusExtension]](ctx, reader, ResourceID)
	if state.IsNotFoundError(err) {
		return Status{}, false, nil
	}
	if err != nil {
		return Status{}, false, err
	}
	return *resource.TypedSpec(), true, nil
}

func WatchStatus(ctx context.Context, reader state.State, condition func(Status) (bool, error)) error {
	_, err := safe.StateWatchFor[*typed.Resource[Status, statusExtension]](ctx, reader, statusResource(Status{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			current, ok := value.(*typed.Resource[Status, statusExtension])
			if !ok {
				return false, fmt.Errorf("unexpected Runner aggregate status resource %T", value)
			}
			return condition(*current.TypedSpec())
		}))
	return err
}

// PublishDesired is the host-side writer for Runner intent. The Runner owns admission locking;
// this function owns only typed publication of the shared desired-state resource.
func PublishDesired(ctx context.Context, writer state.State, value Desired) error {
	resource := desiredResource(value)
	return safe.StateModify(ctx, writer, resource, func(current *typed.Resource[Desired, desiredExtension]) error {
		*current.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner(HostOwner))
}

// WriteStatus is used only by the Runner aggregate controller, which exclusively owns this output.
func WriteStatus(ctx context.Context, writer controller.ReaderWriter, value Status) (bool, error) {
	current, found, err := ReadStatus(ctx, writer)
	if err != nil || found && current == value {
		return false, err
	}
	err = safe.WriterModify(ctx, writer, statusResource(value), func(resource *typed.Resource[Status, statusExtension]) error {
		*resource.TypedSpec() = value
		return nil
	})
	return err == nil, err
}

func desiredResource(value Desired) *typed.Resource[Desired, desiredExtension] {
	return typed.NewResource[Desired, desiredExtension](
		resource.NewMetadata(Namespace, DesiredStateType, ResourceID, resource.VersionUndefined), value,
	)
}

func statusResource(value Status) *typed.Resource[Status, statusExtension] {
	return typed.NewResource[Status, statusExtension](
		resource.NewMetadata(Namespace, AggregateStatusType, ResourceID, resource.VersionUndefined), value,
	)
}
