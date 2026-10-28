package codexappserver

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/cosi"
	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	InputType      = "CodexAppServerInputs.agentplane"
	RuntimeType    = "CodexAppServerRuntimes.agentplane"
	OperationType  = "CodexAppServerOperationResults.agentplane"
	ControllerName = "codex-app-server-lifecycle"
	ResourceID     = "codex-app-server"
	HostOwner      = "codex-app-server-host"
)

type Input struct {
	Demand         uint64
	ExitGeneration uint64
	ExitProcessID  string
	ExitError      string
}

type Phase uint8

const (
	Stopped Phase = iota
	Initializing
	Ready
	Draining
)

type Runtime struct {
	Phase                   Phase
	Generation              uint64
	Attempt                 uint64
	ExitGeneration          uint64
	ExecServerReadyDeadline time.Time
	ProcessID               string
	Error                   string
	StopError               string
}

type operationResult struct {
	Done bool
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
	return typed.NewResource[T, componentExtension[T]](resource.NewMetadata(lifecycle.Namespace, value.resourceType(), id, resource.VersionUndefined), value)
}

func componentRead[T componentSpec[T]](ctx context.Context, reader controller.Reader, id string) (T, bool, error) {
	var zero T
	return cosi.Read(ctx, reader, componentResource(id, zero))
}

func componentWrite[T componentSpec[T]](ctx context.Context, writer controller.ReaderWriter, id string, value T) (bool, error) {
	return cosi.Write(ctx, writer, componentResource(id, value), value)
}

func publish[T componentSpec[T]](ctx context.Context, store state.State, id string, value T) error {
	return cosi.Publish(ctx, store, componentResource(id, value), value, HostOwner)
}

func ReadInput(ctx context.Context, reader controller.Reader) (Input, bool, error) {
	return componentRead[Input](ctx, reader, ResourceID)
}

func ReadRuntime(ctx context.Context, reader controller.Reader) (Runtime, bool, error) {
	return componentRead[Runtime](ctx, reader, ResourceID)
}

func (Input) resourceType() resource.Type               { return InputType }
func (value Input) DeepCopy() Input                     { return value }
func (Runtime) resourceType() resource.Type             { return RuntimeType }
func (value Runtime) DeepCopy() Runtime                 { return value }
func (operationResult) resourceType() resource.Type     { return OperationType }
func (value operationResult) DeepCopy() operationResult { return value }
