package runtime

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
)

const (
	environmentAdmissionCountType  = "EnvironmentAdmissionCounts.agentplane"
	journalSealType                = "JournalSeals.agentplane"
	sessionLifecycleControllerName = "environment-session-lifecycle"
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
	return spec.ResourceDefinitionSpec{Type: value.resourceType(), DefaultNamespace: harnesshostlifecycle.Namespace}
}

func lifecycleResourceFor[T lifecycleSpec[T]](id string, value T) *typed.Resource[T, lifecycleExtension[T]] {
	return typed.NewResource[T, lifecycleExtension[T]](
		resource.NewMetadata(harnesshostlifecycle.Namespace, value.resourceType(), id, resource.VersionUndefined), value,
	)
}

func lifecycleRead[T lifecycleSpec[T]](ctx context.Context, reader controller.Reader, id string) (T, bool, error) {
	var zero T
	return cosi.Read(ctx, reader, lifecycleResourceFor(id, zero))
}

type environmentAdmissionCountSpec struct {
	Admissions int
}
type journalSealSpec struct {
	Present        bool
	IncarnationUID string
	Cursor         uint64
}

func (environmentAdmissionCountSpec) resourceType() resource.Type {
	return environmentAdmissionCountType
}
func (value environmentAdmissionCountSpec) DeepCopy() environmentAdmissionCountSpec { return value }

func (journalSealSpec) resourceType() resource.Type     { return journalSealType }
func (value journalSealSpec) DeepCopy() journalSealSpec { return value }
