package lifecycle

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
)

// Spec values contain only scalars and immutable values, never native handles or mutable references.
type Spec[T any] interface {
	comparable
	typed.DeepCopyable[T]
	ResourceType() resource.Type
}

type Extension[T Spec[T]] struct{}

func (Extension[T]) ResourceDefinition() spec.ResourceDefinitionSpec {
	var value T
	return spec.ResourceDefinitionSpec{Type: value.ResourceType(), DefaultNamespace: Namespace}
}

func ResourceFor[T Spec[T]](id string, value T) *typed.Resource[T, Extension[T]] {
	return typed.NewResource[T, Extension[T]](
		resource.NewMetadata(Namespace, value.ResourceType(), id, resource.VersionUndefined), value,
	)
}

func Read[T Spec[T]](ctx context.Context, reader controller.Reader, id string) (T, bool, error) {
	var zero T
	return cosi.Read(ctx, reader, ResourceFor(id, zero))
}

func Write[T Spec[T]](ctx context.Context, writer controller.ReaderWriter, id string, value T) (bool, error) {
	return cosi.Write(ctx, writer, ResourceFor(id, value), value)
}

func Publish[T Spec[T]](ctx context.Context, writer state.State, id string, value T, owner string) error {
	return cosi.Publish(ctx, writer, ResourceFor(id, value), value, owner)
}
