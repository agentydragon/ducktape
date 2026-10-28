package runner

import (
	"context"
	"fmt"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	lifecycleNamespace    = "runner"
	runnerGoalType        = "RunnerGoals.agentplane"
	runnerInputType       = "RunnerInputs.agentplane"
	runnerRuntimeType     = "RunnerRuntimes.agentplane"
	sessionInputType      = "SessionInputs.agentplane"
	sessionRuntimeType    = "SessionRuntimes.agentplane"
	journalSealType       = "JournalSeals.agentplane"
	ingressAckType        = "IngressAcks.agentplane"
	operationResultType   = "NativeOperationResults.agentplane"
	runnerControllerName  = "runner-lifecycle"
	sessionControllerName = "session-lifecycle"
)

// Specs contain scalar values and immutable time.Time values; no native handles or mutable references.
type lifecycleSpec[T any] interface {
	comparable
	typed.DeepCopyable[T]
	resourceType() resource.Type
}

type lifecycleExtension[T lifecycleSpec[T]] struct{}

func (lifecycleExtension[T]) ResourceDefinition() spec.ResourceDefinitionSpec {
	var value T
	return spec.ResourceDefinitionSpec{Type: value.resourceType(), DefaultNamespace: lifecycleNamespace}
}

func lifecycleResourceFor[T lifecycleSpec[T]](id string, value T) *typed.Resource[T, lifecycleExtension[T]] {
	return typed.NewResource[T, lifecycleExtension[T]](
		resource.NewMetadata(lifecycleNamespace, value.resourceType(), id, resource.VersionUndefined), value,
	)
}

func lifecycleRead[T lifecycleSpec[T]](ctx context.Context, reader controller.Reader, id string) (T, bool, error) {
	var zero T
	r, err := safe.ReaderGetByID[*typed.Resource[T, lifecycleExtension[T]]](ctx, reader, id)
	if state.IsNotFoundError(err) {
		return zero, false, nil
	}
	if err != nil {
		return zero, false, err
	}
	return *r.TypedSpec(), true, nil
}

func lifecycleWrite[T lifecycleSpec[T]](ctx context.Context, writer controller.ReaderWriter, id string, value T) (bool, error) {
	old, found, err := lifecycleRead[T](ctx, writer, id)
	if err != nil || found && old == value {
		return false, err
	}
	err = safe.WriterModify(ctx, writer, lifecycleResourceFor(id, value), func(r *typed.Resource[T, lifecycleExtension[T]]) error {
		*r.TypedSpec() = value
		return nil
	})
	return err == nil, err
}

type runnerMode uint8

const (
	runnerRunning runnerMode = iota
	runnerQuiescing
	runnerClosing
)

type runnerStage uint8

const (
	stageRunning runnerStage = iota
	stageDraining
	stageHalting
	stageStopping
	stageFencing
	stageSealing
	stageFlushing
	stageQuiesced
	stageClosed
)

type runnerGoalSpec struct {
	Mode       runnerMode
	SealUID    string
	Generation uint64
}
type runnerInputSpec struct {
	Revision   uint64
	Admissions int
}
type runnerRuntimeSpec struct {
	Generation uint64
	Stage      runnerStage
	Error      string
}
type executionPhase uint8

const (
	executionDormant executionPhase = iota
	executionPreparing
	executionReady
	executionStopping
	executionHalted
)

type sessionRuntimeSpec struct {
	Phase          executionPhase
	Attempt        uint64
	Resuming       bool
	ProcessID      string
	Generation     uint64
	SetupAttempt   string
	ProcessStopped bool
	Error          string
}
type sessionInputSpec struct {
	Demand            uint64
	Cursor            uint64
	SetupRequested    bool
	Setup             runnerpb.SetupState
	ActiveTurn        string
	Pending           int
	ProcessID         string
	ProcessRunning    bool
	Inspections       int
	InspectionRunning bool
	InspectionReady   bool
}
type operationResultSpec struct {
	Session    string
	Controller string
	Done       bool
}
type journalSealSpec struct {
	Present        bool
	IncarnationUID string
	Cursor         uint64
}
type ingressAckSpec struct {
	Cursor uint64
	Sealed bool
}

func (runnerGoalSpec) resourceType() resource.Type    { return runnerGoalType }
func (value runnerGoalSpec) DeepCopy() runnerGoalSpec { return value }

func (runnerInputSpec) resourceType() resource.Type     { return runnerInputType }
func (value runnerInputSpec) DeepCopy() runnerInputSpec { return value }

func (runnerRuntimeSpec) resourceType() resource.Type       { return runnerRuntimeType }
func (value runnerRuntimeSpec) DeepCopy() runnerRuntimeSpec { return value }

func (sessionRuntimeSpec) resourceType() resource.Type        { return sessionRuntimeType }
func (value sessionRuntimeSpec) DeepCopy() sessionRuntimeSpec { return value }

func (sessionInputSpec) resourceType() resource.Type      { return sessionInputType }
func (value sessionInputSpec) DeepCopy() sessionInputSpec { return value }

func (operationResultSpec) resourceType() resource.Type         { return operationResultType }
func (value operationResultSpec) DeepCopy() operationResultSpec { return value }

func (journalSealSpec) resourceType() resource.Type     { return journalSealType }
func (value journalSealSpec) DeepCopy() journalSealSpec { return value }

func (ingressAckSpec) resourceType() resource.Type    { return ingressAckType }
func (value ingressAckSpec) DeepCopy() ingressAckSpec { return value }

func watchLifecycle[T lifecycleSpec[T]](ctx context.Context, st state.State, id string, condition func(T) (bool, error)) error {
	var zero T
	_, err := safe.StateWatchFor[*typed.Resource[T, lifecycleExtension[T]]](ctx, st, lifecycleResourceFor(id, zero).Metadata(),
		state.WithCondition(func(r resource.Resource) (bool, error) {
			value, ok := r.(*typed.Resource[T, lifecycleExtension[T]])
			if !ok {
				return false, fmt.Errorf("unexpected lifecycle resource %T", r)
			}
			return condition(*value.TypedSpec())
		}))
	return err
}
