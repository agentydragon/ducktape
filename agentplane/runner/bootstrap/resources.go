// Package bootstrap owns a Sandbox's durable one-shot initialization attempt.
package bootstrap

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/runner/cosi"
	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
)

const (
	InputType      = "BootstrapInputs.agentplane"
	RuntimeType    = "BootstrapRuntimes.agentplane"
	OperationType  = "BootstrapOperationResults.agentplane"
	ControllerName = "bootstrap-lifecycle"
	ResourceID     = "bootstrap"
	HostOwner      = "bootstrap-host"
)

// Input records that the Runner synchronously admitted the Sandbox bootstrap.
// The retained request file and event log remain the durable authorities.
type Input struct {
	Requested bool
}

type Phase uint8

const (
	Dormant Phase = iota
	Running
	Succeeded
	Failed
	Interrupted
)

type Runtime struct {
	Phase Phase
	Error string
}

type operationResult struct{ Done bool }

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
	return cosi.Read(ctx, reader, componentResource(id, zero))
}

func componentWrite[T componentSpec[T]](ctx context.Context, writer controller.ReaderWriter, id string, value T) (bool, error) {
	return cosi.Write(ctx, writer, componentResource(id, value), value)
}

func publish[T componentSpec[T]](ctx context.Context, writer state.State, id string, value T) error {
	return cosi.Publish(ctx, writer, componentResource(id, value), value, HostOwner)
}

func ReadInput(ctx context.Context, reader controller.Reader) (Input, bool, error) {
	return componentRead[Input](ctx, reader, ResourceID)
}

func ReadRuntime(ctx context.Context, reader controller.Reader) (Runtime, bool, error) {
	return componentRead[Runtime](ctx, reader, ResourceID)
}

func isTerminal(phase Phase) bool {
	return phase == Succeeded || phase == Failed || phase == Interrupted
}

func (Input) resourceType() resource.Type               { return InputType }
func (value Input) DeepCopy() Input                     { return value }
func (Runtime) resourceType() resource.Type             { return RuntimeType }
func (value Runtime) DeepCopy() Runtime                 { return value }
func (operationResult) resourceType() resource.Type     { return OperationType }
func (value operationResult) DeepCopy() operationResult { return value }
