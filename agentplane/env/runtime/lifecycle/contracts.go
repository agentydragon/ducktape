// Package lifecycle defines Runtime desired state and component lifecycle observations.
package lifecycle

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	Namespace                   = "environment-runtime"
	AdmissionCountType          = "EnvironmentAdmissionCounts.agentplane"
	AggregateStatusType         = "EnvironmentSessionStatuses.agentplane"
	ResourceID                  = "environment-runtime"
	SessionOwner                = "environment-sessions"
	EnvironmentDesiredStateType = "EnvironmentRuntimeDesiredStates.agentplane"
	// ExecutionStatusType is observed by the app-server controller to
	// wake its live Execution readiness check.
	ExecutionStatusType   = "ExecutionStatuses.agentplane"
	ExecutionResourceID   = "execution"
	EnvironmentResourceID = "environment-runtime"
	EnvironmentOwner      = "environment-runtime"
)

type Mode uint8

const (
	Inactive Mode = iota
	Running
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

// EnvironmentDesired is the only environment lifecycle desire. Runtime revokes
// admission before publishing a replacement and accepts the exact revision only
// after its local recovery work completes.
type EnvironmentDesired struct {
	Mode           Mode
	IncarnationUID string
	SealUID        string
	Generation     uint64
}

func (value EnvironmentDesired) DeepCopy() EnvironmentDesired { return value }

type EnvironmentAdmissionGuard func(ctx context.Context, expected EnvironmentDesired, effect func(context.Context) error) error

type environmentDesiredExtension struct{}

func (environmentDesiredExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: EnvironmentDesiredStateType, DefaultNamespace: Namespace}
}

func ReadEnvironmentDesired(ctx context.Context, reader controller.Reader) (EnvironmentDesired, bool, error) {
	value, err := safe.ReaderGetByID[*typed.Resource[EnvironmentDesired, environmentDesiredExtension]](ctx, reader, EnvironmentResourceID)
	if state.IsNotFoundError(err) {
		return EnvironmentDesired{}, false, nil
	}
	if err != nil {
		return EnvironmentDesired{}, false, err
	}
	return *value.TypedSpec(), true, nil
}

func PublishEnvironmentDesired(ctx context.Context, writer state.State, value EnvironmentDesired) error {
	resource := typed.NewResource[EnvironmentDesired, environmentDesiredExtension](
		resource.NewMetadata(Namespace, EnvironmentDesiredStateType, EnvironmentResourceID, resource.VersionUndefined), value,
	)
	return cosi.Publish(ctx, writer, resource, value, EnvironmentOwner)
}

// Blocker identifies the first unmet predicate in the aggregate Session barrier. It is written with
// the corresponding Stage and describes the resources the controller just inspected; it is not an
// independent lifecycle state machine.
type BlockerKind uint8

const (
	NoBlocker BlockerKind = iota
	AdmissionsPending
	SessionHaltPending
	SessionProcessPending
	CodexAppServerPending
	CodexExecServerPending
	BootstrapPending
	ProcessCleanupPending
	SessionInspectionPending
	JournalSealPending
	IngressAcknowledgementPending
)

type Blocker struct {
	Kind                   BlockerKind
	Error                  string
	ResourceType           string
	ResourceID             string
	ProcessID              string
	ResourcePresent        bool
	Admissions             uint64
	JournalSealPresent     bool
	JournalSealUID         string
	ExpectedJournalSealUID string
	JournalSealCursor      uint64
	AcknowledgementPresent bool
	AcknowledgementSealed  bool
	AcknowledgementCursor  uint64
	FinalizersPending      bool
}

func (value Blocker) DeepCopy() Blocker { return value }

type Status struct {
	Generation    uint64
	Stage         Stage
	Error         string
	Blocker       Blocker
	NativeStopped bool
}

func (value Status) DeepCopy() Status { return value }

type statusExtension struct{}

func (statusExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: AggregateStatusType, DefaultNamespace: Namespace}
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
				return false, fmt.Errorf("unexpected Session aggregate status resource %T", value)
			}
			return condition(*current.TypedSpec())
		}))
	return err
}

// WriteStatus is used only by the Session aggregate controller, which exclusively owns this output.
func WriteStatus(ctx context.Context, writer controller.ReaderWriter, value Status) (bool, error) {
	return cosi.Write(ctx, writer, statusResource(value), value)
}

func statusResource(value Status) *typed.Resource[Status, statusExtension] {
	return typed.NewResource[Status, statusExtension](
		resource.NewMetadata(Namespace, AggregateStatusType, ResourceID, resource.VersionUndefined), value,
	)
}
