package cosistate

import (
	"context"
	"errors"
	"fmt"

	persistencepb "github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate/persistentpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	resourcepb "github.com/cosi-project/runtime/pkg/resource/protobuf"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

const EnvironmentSealType = "EnvironmentSeals.agentplane"

type EnvironmentSeal struct {
	IncarnationUID string
}

func (value EnvironmentSeal) DeepCopy() EnvironmentSeal { return value }

type environmentSealExtension struct{}

func (environmentSealExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: EnvironmentSealType, DefaultNamespace: lifecycle.Namespace}
}

type (
	environmentSealSpec     = resourcepb.ResourceSpec[persistencepb.EnvironmentSealSpec, *persistencepb.EnvironmentSealSpec]
	EnvironmentSealResource = typed.Resource[environmentSealSpec, environmentSealExtension]
)

func init() {
	if err := resourcepb.RegisterResource(EnvironmentSealType, &EnvironmentSealResource{}); err != nil {
		panic(err)
	}
}

func EnvironmentSealResourceValue(value EnvironmentSeal) *EnvironmentSealResource {
	return typed.NewResource[environmentSealSpec, environmentSealExtension](
		resource.NewMetadata(lifecycle.Namespace, EnvironmentSealType, lifecycle.EnvironmentResourceID, resource.VersionUndefined),
		resourcepb.NewResourceSpec(&persistencepb.EnvironmentSealSpec{IncarnationUid: value.IncarnationUID}),
	)
}

func EnvironmentSealRegistration() Registration {
	return Registration{Type: EnvironmentSealType, Validate: validateEnvironmentSealResource}
}

func ReadEnvironmentSeal(ctx context.Context, reader controller.Reader) (EnvironmentSeal, bool, error) {
	value, err := safe.ReaderGet[*EnvironmentSealResource](ctx, reader, EnvironmentSealResourceValue(EnvironmentSeal{}).Metadata())
	if state.IsNotFoundError(err) {
		return EnvironmentSeal{}, false, nil
	}
	if err != nil {
		return EnvironmentSeal{}, false, err
	}
	if err := validateEnvironmentSealResource(value); err != nil {
		return EnvironmentSeal{}, false, err
	}
	return EnvironmentSeal{IncarnationUID: value.TypedSpec().Value.IncarnationUid}, true, nil
}

func WriteEnvironmentSeal(ctx context.Context, writer state.State, value EnvironmentSeal) error {
	if value.IncarnationUID == "" {
		return errors.New("environment incarnation UID is required")
	}
	return safe.StateModify(ctx, writer, EnvironmentSealResourceValue(value), func(current *EnvironmentSealResource) error {
		current.TypedSpec().Value = &persistencepb.EnvironmentSealSpec{IncarnationUid: value.IncarnationUID}
		return nil
	}, state.WithUpdateOwner(lifecycle.EnvironmentOwner))
}

func DeleteEnvironmentSeal(ctx context.Context, writer state.State, incarnationUID string) error {
	seal, found, err := ReadEnvironmentSeal(ctx, writer)
	if err != nil || !found {
		return err
	}
	if seal.IncarnationUID != incarnationUID {
		return fmt.Errorf("environment seal UID %q does not match expected incarnation %q", seal.IncarnationUID, incarnationUID)
	}
	return writer.Destroy(ctx, EnvironmentSealResourceValue(seal).Metadata(), state.WithDestroyOwner(lifecycle.EnvironmentOwner))
}

func validateEnvironmentSealResource(value resource.Resource) error {
	seal, ok := value.(*EnvironmentSealResource)
	if !ok || seal.Metadata().Namespace() != lifecycle.Namespace || seal.Metadata().Type() != EnvironmentSealType ||
		seal.Metadata().ID() != lifecycle.EnvironmentResourceID || seal.Metadata().Owner() != lifecycle.EnvironmentOwner ||
		seal.TypedSpec().Value == nil {
		return fmt.Errorf("unexpected environment seal resource %T", value)
	}
	if seal.TypedSpec().Value.IncarnationUid == "" {
		return errors.New("environment seal has no incarnation UID")
	}
	return nil
}
