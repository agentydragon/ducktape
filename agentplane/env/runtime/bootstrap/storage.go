package bootstrap

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	persistencepb "github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate/persistentpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
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
	eventSpec         = resourcepb.ResourceSpec[persistencepb.BootstrapEventSpec, *persistencepb.BootstrapEventSpec]
	EventResource     = typed.Resource[eventSpec, eventExtension]
)

type eventExtension struct{}

func (eventExtension) ResourceDefinition() spec.ResourceDefinitionSpec {
	return spec.ResourceDefinitionSpec{Type: EventType, DefaultNamespace: lifecycle.Namespace}
}

func init() {
	if err := resourcepb.RegisterResource(SelectionType, &SelectionResource{}); err != nil {
		panic(err)
	}
	if err := resourcepb.RegisterResource(EventType, &EventResource{}); err != nil {
		panic(err)
	}
}

func SelectionResourceValue(script string) *SelectionResource {
	return typed.NewResource[selectionSpec, selectionExtension](resource.NewMetadata(lifecycle.Namespace, SelectionType, ResourceID, resource.VersionUndefined),
		resourcepb.NewResourceSpec(&persistencepb.BootstrapSelectionSpec{Script: []byte(script)}))
}

func SelectionRegistration() cosistate.Registration {
	return cosistate.Registration{Type: SelectionType, Validate: validateSelectionResource}
}

func EventRegistration() cosistate.Registration {
	return cosistate.Registration{Type: EventType, Validate: validateEventResource}
}

func validateSelectionResource(value resource.Resource) error {
	selected, ok := value.(*SelectionResource)
	if !ok || selected.Metadata().Namespace() != lifecycle.Namespace || selected.Metadata().Type() != SelectionType || selected.Metadata().ID() != ResourceID || selected.Metadata().Owner() != HostOwner || selected.TypedSpec().Value == nil {
		return fmt.Errorf("unexpected bootstrap selection resource %T", value)
	}
	return validateScript(string(selected.TypedSpec().Value.Script))
}

func EventResourceValue(event *sessionpb.InitializationEvent) *EventResource {
	var sequence uint64
	if event != nil {
		sequence = event.GetSequence()
	}
	return typed.NewResource[eventSpec, eventExtension](
		resource.NewMetadata(lifecycle.Namespace, EventType, eventResourceID(sequence), resource.VersionUndefined),
		resourcepb.NewResourceSpec(&persistencepb.BootstrapEventSpec{Event: event}),
	)
}

func eventResourceID(sequence uint64) string {
	return fmt.Sprintf("%s-%020d", ResourceID, sequence)
}

func validateEventResource(value resource.Resource) error {
	eventResource, ok := value.(*EventResource)
	if !ok || eventResource.Metadata().Namespace() != lifecycle.Namespace || eventResource.Metadata().Type() != EventType || eventResource.Metadata().Owner() != HostOwner || eventResource.TypedSpec().Value == nil || eventResource.TypedSpec().Value.Event == nil {
		return fmt.Errorf("unexpected bootstrap event resource %T", value)
	}
	event := eventResource.TypedSpec().Value.Event
	sequence, err := strconv.ParseUint(strings.TrimPrefix(eventResource.Metadata().ID(), ResourceID+"-"), 10, 64)
	if err != nil || sequence == 0 || eventResource.Metadata().ID() != eventResourceID(sequence) || event.GetSequence() != sequence {
		return fmt.Errorf("bootstrap event resource %q has an invalid sequence", eventResource.Metadata().ID())
	}
	if err := validateEventObservation(event); err != nil {
		return fmt.Errorf("invalid bootstrap event %d: %w", sequence, err)
	}
	return nil
}
