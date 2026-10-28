package codexexecserver

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	InputType      = "CodexExecServerInputs.agentplane"
	RuntimeType    = "CodexExecServerRuntimes.agentplane"
	OperationType  = "CodexExecServerOperationResults.agentplane"
	ControllerName = "codex-exec-server-lifecycle"
	ResourceID     = "codex-exec-server"
	HostOwner      = "codex-exec-server-host"
)

type Input struct{ Demand uint64 }

type Phase uint8

const (
	Stopped Phase = iota
	Starting
	Probing
	Ready
	Stopping
	Retrying
)

type Runtime struct {
	ProcessID                string
	Phase                    Phase
	Generation               uint64
	FencedGeneration         uint64
	Error                    string
	StopError                string
	Refusal                  string
	RestartFailures          uint32
	ConsecutiveProbeFailures uint8
	ReadyDeadline            time.Time
	NextProbe                time.Time
	RetryAt                  time.Time
}

type operationResult struct {
	Controller string
	Done       bool
}

func (value Input) DeepCopy() Input { return value }

func (value Runtime) DeepCopy() Runtime { return value }

func (value operationResult) DeepCopy() operationResult { return value }

func (Input) resourceType() resource.Type { return InputType }

func (Runtime) resourceType() resource.Type { return RuntimeType }

func (operationResult) resourceType() resource.Type { return OperationType }

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
	err = safe.WriterModify(ctx, writer, componentResource(id, value), func(resource *typed.Resource[T, componentExtension[T]]) error {
		*resource.TypedSpec() = value
		return nil
	})
	return err == nil, err
}

func ReadInput(ctx context.Context, reader controller.Reader) (Input, bool, error) {
	return componentRead[Input](ctx, reader, ResourceID)
}

func ReadRuntime(ctx context.Context, reader controller.Reader) (Runtime, bool, error) {
	return componentRead[Runtime](ctx, reader, ResourceID)
}

func readOperation(ctx context.Context, reader controller.Reader, id string) (operationResult, bool, error) {
	return componentRead[operationResult](ctx, reader, id)
}
