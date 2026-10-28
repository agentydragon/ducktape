package runner

import (
	"context"
	"fmt"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
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

// Specs contain values only; native handles and cancellation belong to operation hosts.
type lifecycleResource[T comparable] struct {
	metadata resource.Metadata
	value    T
}

func (r *lifecycleResource[T]) Metadata() *resource.Metadata { return &r.metadata }
func (r *lifecycleResource[T]) Spec() any                    { return &r.value }
func (r *lifecycleResource[T]) DeepCopy() resource.Resource {
	return &lifecycleResource[T]{metadata: r.metadata.Copy(), value: r.value}
}

func lifecycleResourceFor[T comparable](kind, id string, value T) *lifecycleResource[T] {
	return &lifecycleResource[T]{metadata: resource.NewMetadata(lifecycleNamespace, kind, id, resource.VersionUndefined), value: value}
}

type lifecycleReader interface {
	Get(context.Context, resource.Pointer, ...state.GetOption) (resource.Resource, error)
}

func lifecycleRead[T comparable](ctx context.Context, reader lifecycleReader, kind, id string) (T, bool, error) {
	var zero T
	r, err := reader.Get(ctx, resource.NewMetadata(lifecycleNamespace, kind, id, resource.VersionUndefined))
	if state.IsNotFoundError(err) {
		return zero, false, nil
	}
	if err != nil {
		return zero, false, err
	}
	typed, ok := r.(*lifecycleResource[T])
	if !ok {
		return zero, false, fmt.Errorf("unexpected lifecycle resource %T for %s/%s", r, kind, id)
	}
	return typed.value, true, nil
}

func lifecycleWrite[T comparable](ctx context.Context, writer controller.ReaderWriter, kind, id string, value T) (bool, error) {
	old, found, err := lifecycleRead[T](ctx, writer, kind, id)
	if err != nil || found && old == value {
		return false, err
	}
	err = writer.Modify(ctx, lifecycleResourceFor(kind, id, value), func(r resource.Resource) error {
		r.(*lifecycleResource[T]).value = value
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
	Session string
	Kind    string
	Done    bool
	Error   string
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
