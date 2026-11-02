package bootstrap

import (
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
)

// Selection is a singleton and never updated. Its fixed COSI metadata is
// reconstructed when the persisted selection is loaded.
type selectionMarshaler struct{}

type selectionDisk struct {
	Script []byte `json:"script"`
}

func SelectionCodec() cosistate.Codec {
	codec := selectionMarshaler{}
	return cosistate.Codec{Type: SelectionType, Marshal: codec.MarshalResource, Unmarshal: codec.UnmarshalResource}
}

func (selectionMarshaler) MarshalResource(value resource.Resource) ([]byte, error) {
	selected, ok := value.(*typed.Resource[Selection, componentExtension[Selection]])
	if !ok || selected.Metadata().ID() != ResourceID {
		return nil, fmt.Errorf("unexpected bootstrap selection resource %T", value)
	}
	if err := validateScript(selected.TypedSpec().Script); err != nil {
		return nil, err
	}
	return json.Marshal(selectionDisk{Script: []byte(selected.TypedSpec().Script)})
}

func (selectionMarshaler) UnmarshalResource(data []byte) (resource.Resource, error) {
	var disk selectionDisk
	if err := decodeSelection(data, &disk); err != nil {
		return nil, err
	}
	selected := Selection{Script: string(disk.Script)}
	if err := validateScript(selected.Script); err != nil {
		return nil, err
	}
	value := componentResource(ResourceID, selected)
	value.Metadata().SetVersion(resource.VersionUndefined.Next())
	if err := value.Metadata().SetOwner(HostOwner); err != nil {
		return nil, err
	}
	return value, nil
}
