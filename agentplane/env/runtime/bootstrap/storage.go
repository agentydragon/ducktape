package bootstrap

import (
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	persistencepb "github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate/persistentpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	resourcepb "github.com/cosi-project/runtime/pkg/resource/protobuf"
	"github.com/cosi-project/runtime/pkg/resource/typed"
)

type selectionExtension struct{}

func (selectionExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: SelectionType, DefaultNamespace: lifecycle.Namespace}
}

type (
	selectionSpec     = resourcepb.ResourceSpec[persistencepb.BootstrapSelectionSpec, *persistencepb.BootstrapSelectionSpec]
	SelectionResource = typed.Resource[selectionSpec, selectionExtension]
)

func init() {
	if err := resourcepb.RegisterResource(SelectionType, &SelectionResource{}); err != nil {
		panic(err)
	}
}

func SelectionResourceValue(value Selection) *SelectionResource {
	return typed.NewResource[selectionSpec, selectionExtension](resource.NewMetadata(lifecycle.Namespace, SelectionType, ResourceID, resource.VersionUndefined),
		resourcepb.NewResourceSpec(&persistencepb.BootstrapSelectionSpec{Script: []byte(value.Script)}))
}

func SelectionRegistration() cosistate.Registration {
	return cosistate.Registration{Type: SelectionType, Validate: validateSelectionResource}
}

func validateSelectionResource(value resource.Resource) error {
	selected, ok := value.(*SelectionResource)
	if !ok || selected.Metadata().Namespace() != lifecycle.Namespace || selected.Metadata().Type() != SelectionType || selected.Metadata().ID() != ResourceID || selected.Metadata().Owner() != HostOwner || selected.TypedSpec().Value == nil {
		return fmt.Errorf("unexpected bootstrap selection resource %T", value)
	}
	return validateScript(string(selected.TypedSpec().Value.Script))
}
