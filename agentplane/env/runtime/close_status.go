package runtime

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const runtimeCloseStatusType = "EnvironmentRuntimeCloseStatuses.agentplane"

// RuntimeCloseStatus is a live observation of the Close worker, not owner release.
// Failure stays as an error because the in-process Close caller must retain its
// identity (including errors.Is); this resource has no durable registration.
type RuntimeCloseStatus struct {
	Generation uint64
	Stage      runtimeCloseStage
	Failure    error
}

func (value RuntimeCloseStatus) DeepCopy() RuntimeCloseStatus { return value }

type runtimeCloseStatusExtension struct{}

func (runtimeCloseStatusExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: runtimeCloseStatusType, DefaultNamespace: lifecycle.Namespace}
}

func runtimeCloseStatusResource(value RuntimeCloseStatus) *typed.Resource[RuntimeCloseStatus, runtimeCloseStatusExtension] {
	return typed.NewResource[RuntimeCloseStatus, runtimeCloseStatusExtension](
		resource.NewMetadata(lifecycle.Namespace, runtimeCloseStatusType, lifecycle.ResourceID, resource.VersionUndefined), value,
	)
}

func readRuntimeCloseStatus(ctx context.Context, reader controller.Reader) (RuntimeCloseStatus, bool, error) {
	value, err := safe.ReaderGetByID[*typed.Resource[RuntimeCloseStatus, runtimeCloseStatusExtension]](ctx, reader, lifecycle.ResourceID)
	if state.IsNotFoundError(err) {
		return RuntimeCloseStatus{}, false, nil
	}
	if err != nil {
		return RuntimeCloseStatus{}, false, err
	}
	return *value.TypedSpec(), true, nil
}

func writeRuntimeCloseStatus(ctx context.Context, writer controller.Writer, value RuntimeCloseStatus) error {
	// Do not compare error interfaces: a caller-supplied error may have a
	// non-comparable dynamic type. The controller only writes on stage changes.
	return safe.WriterModify(ctx, writer, runtimeCloseStatusResource(value), func(current *typed.Resource[RuntimeCloseStatus, runtimeCloseStatusExtension]) error {
		*current.TypedSpec() = value
		return nil
	})
}
