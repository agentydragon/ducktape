// Package lifecycle defines distinct Runtime and Host desired resources and
// the Host aggregate status consumed by their coordinated lifecycle.
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
	Namespace                    = "environment-runtime"
	DesiredStateType             = "RunnerDesiredStates.agentplane"
	AggregateStatusType          = "RunnerRuntimes.agentplane"
	RuntimeCloseStatusType       = "EnvironmentRuntimeCloseStatuses.agentplane"
	RuntimeTransitionRequestType = "EnvironmentRuntimeTransitionRequests.agentplane"
	RuntimeTransitionStatusType  = "EnvironmentRuntimeTransitionStatuses.agentplane"
	ResourceID                   = "runner"
	HostOwner                    = "runner-host"
	EnvironmentDesiredStateType  = "EnvironmentRuntimeDesiredStates.agentplane"
	// ExecutionStatusType is observed by the app-server controller to
	// wake its live Execution readiness check.
	ExecutionStatusType   = "ExecutionStatuses.agentplane"
	ExecutionResourceID   = "execution"
	EnvironmentResourceID = "environment-runtime"
	EnvironmentOwner      = "environment-runtime"
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

// EnvironmentDesired is Runtime's bootstrap and process admission generation.
// Host Desired is a separate resource with its own writer and lifecycle.
type EnvironmentDesired struct {
	Mode       Mode
	SealUID    string
	Generation uint64
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
	return safe.StateModify(ctx, writer, resource, func(current *typed.Resource[EnvironmentDesired, environmentDesiredExtension]) error {
		*current.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner(EnvironmentOwner))
}

func (value Desired) DeepCopy() Desired { return value }

// Blocker identifies the first unmet predicate in the aggregate Runner barrier. It is written with
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
	Generation uint64
	Stage      Stage
	Error      string
	Blocker    Blocker
}

// RuntimeCloseStage is the Runtime's ordered shutdown barrier. ReadyToJoin means
// the COSI engine can stop; the caller still waits for worker joins and owner release.
type RuntimeCloseStage uint8

const (
	RuntimeClosePreparing RuntimeCloseStage = iota
	RuntimeCloseNativeStopping
	RuntimeCloseExecutionFencing
	RuntimeCloseHostFlushing
	RuntimeCloseHostJoining
	RuntimeCloseBootstrapSettling
	RuntimeCloseReadyToJoin
	RuntimeCloseFailed
)

type RuntimeCloseStatus struct {
	Generation uint64
	Stage      RuntimeCloseStage
	Error      string
}

type RuntimeTransitionKind uint8

const (
	RuntimeAdopt RuntimeTransitionKind = iota + 1
	RuntimeQuiesce
)

type RuntimeTransitionRequest struct {
	Sequence        uint64
	Kind            RuntimeTransitionKind
	UID             string
	HasIdleSince    bool
	IdleSinceMicros int64
}

func (value RuntimeTransitionRequest) DeepCopy() RuntimeTransitionRequest { return value }

type RuntimeTransitionStage uint8

const (
	RuntimeTransitionPlanning RuntimeTransitionStage = iota
	RuntimeTransitionNativeStopping
	RuntimeTransitionExecutionStarting
	RuntimeTransitionIntentPublishing
	RuntimeTransitionExecutionFencing
	RuntimeTransitionHostAcknowledging
	RuntimeTransitionIncarnationAdopting
	RuntimeTransitionHostReady
	RuntimeTransitionBootstrapRequesting
	RuntimeTransitionQuiesceSealing
	RuntimeTransitionHostStatusChecking
	RuntimeTransitionDone
	RuntimeTransitionFailed
)

type RuntimeTransitionStatus struct {
	Sequence  uint64
	Stage     RuntimeTransitionStage
	Error     string
	ErrorCode uint32
}

func (value RuntimeTransitionStatus) DeepCopy() RuntimeTransitionStatus { return value }

type runtimeTransitionRequestExtension struct{}

func (runtimeTransitionRequestExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: RuntimeTransitionRequestType, DefaultNamespace: Namespace}
}

type runtimeTransitionStatusExtension struct{}

func (runtimeTransitionStatusExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: RuntimeTransitionStatusType, DefaultNamespace: Namespace}
}

func ReadRuntimeTransitionRequest(ctx context.Context, reader controller.Reader) (RuntimeTransitionRequest, bool, error) {
	value, err := safe.ReaderGetByID[*typed.Resource[RuntimeTransitionRequest, runtimeTransitionRequestExtension]](ctx, reader, EnvironmentResourceID)
	if state.IsNotFoundError(err) {
		return RuntimeTransitionRequest{}, false, nil
	}
	if err != nil {
		return RuntimeTransitionRequest{}, false, err
	}
	return *value.TypedSpec(), true, nil
}

func PublishRuntimeTransitionRequest(ctx context.Context, writer state.State, value RuntimeTransitionRequest) error {
	resource := typed.NewResource[RuntimeTransitionRequest, runtimeTransitionRequestExtension](
		resource.NewMetadata(Namespace, RuntimeTransitionRequestType, EnvironmentResourceID, resource.VersionUndefined), value,
	)
	return safe.StateModify(ctx, writer, resource, func(current *typed.Resource[RuntimeTransitionRequest, runtimeTransitionRequestExtension]) error {
		*current.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner(EnvironmentOwner))
}

func ReadRuntimeTransitionStatus(ctx context.Context, reader controller.Reader) (RuntimeTransitionStatus, bool, error) {
	value, err := safe.ReaderGetByID[*typed.Resource[RuntimeTransitionStatus, runtimeTransitionStatusExtension]](ctx, reader, EnvironmentResourceID)
	if state.IsNotFoundError(err) {
		return RuntimeTransitionStatus{}, false, nil
	}
	if err != nil {
		return RuntimeTransitionStatus{}, false, err
	}
	return *value.TypedSpec(), true, nil
}

func WriteRuntimeTransitionStatus(ctx context.Context, writer controller.ReaderWriter, value RuntimeTransitionStatus) error {
	current, found, err := ReadRuntimeTransitionStatus(ctx, writer)
	if err != nil || found && current == value {
		return err
	}
	resource := typed.NewResource[RuntimeTransitionStatus, runtimeTransitionStatusExtension](
		resource.NewMetadata(Namespace, RuntimeTransitionStatusType, EnvironmentResourceID, resource.VersionUndefined), value,
	)
	return safe.WriterModify(ctx, writer, resource, func(current *typed.Resource[RuntimeTransitionStatus, runtimeTransitionStatusExtension]) error {
		*current.TypedSpec() = value
		return nil
	})
}

func (value RuntimeCloseStatus) DeepCopy() RuntimeCloseStatus { return value }

type runtimeCloseStatusExtension struct{}

func (runtimeCloseStatusExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: RuntimeCloseStatusType, DefaultNamespace: Namespace}
}

func ReadRuntimeCloseStatus(ctx context.Context, reader controller.Reader) (RuntimeCloseStatus, bool, error) {
	value, err := safe.ReaderGetByID[*typed.Resource[RuntimeCloseStatus, runtimeCloseStatusExtension]](ctx, reader, EnvironmentResourceID)
	if state.IsNotFoundError(err) {
		return RuntimeCloseStatus{}, false, nil
	}
	if err != nil {
		return RuntimeCloseStatus{}, false, err
	}
	return *value.TypedSpec(), true, nil
}

// WriteRuntimeCloseStatus is exclusive to the Runtime aggregate controller.
func WriteRuntimeCloseStatus(ctx context.Context, writer controller.ReaderWriter, value RuntimeCloseStatus) error {
	current, found, err := ReadRuntimeCloseStatus(ctx, writer)
	if err != nil || found && current == value {
		return err
	}
	resource := typed.NewResource[RuntimeCloseStatus, runtimeCloseStatusExtension](
		resource.NewMetadata(Namespace, RuntimeCloseStatusType, EnvironmentResourceID, resource.VersionUndefined), value,
	)
	return safe.WriterModify(ctx, writer, resource, func(current *typed.Resource[RuntimeCloseStatus, runtimeCloseStatusExtension]) error {
		*current.TypedSpec() = value
		return nil
	})
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
