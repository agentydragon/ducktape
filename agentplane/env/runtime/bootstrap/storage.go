package bootstrap

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/cosi-project/runtime/pkg/state/impl/store/bolt"
	"go.etcd.io/bbolt"
)

// OpenState keeps the existing COSI namespace, but backs only the immutable
// bootstrap selection. A fresh process never restores admission or process facts.
func OpenState(stateDir string) (state.State, *bolt.BackingStore, error) {
	backing, err := bolt.NewBackingStore(func() (*bbolt.DB, error) {
		return bbolt.Open(filepath.Join(stateDir, "bootstrap-selections.db"), 0o600, nil)
	}, selectionMarshaler{})
	if err != nil {
		return nil, nil, err
	}
	// The first transaction is durable only if the database directory entry is too.
	directory, err := os.Open(stateDir)
	if err == nil {
		err = directory.Sync()
		err = errors.Join(err, directory.Close())
	}
	if err != nil {
		return nil, nil, errors.Join(err, backing.Close())
	}
	selectionStore := selectionBackingStore{BackingStore: backing.WithNamespace(lifecycle.Namespace)}
	build := func(namespace resource.Namespace) state.CoreState {
		if namespace == lifecycle.Namespace {
			return inmem.NewStateWithOptions(inmem.WithBackingStore(selectionStore))(namespace)
		}
		return inmem.Build(namespace)
	}
	return state.WrapCore(namespaced.NewState(build)), backing, nil
}

type selectionBackingStore struct{ inmem.BackingStore }

func (s selectionBackingStore) Load(ctx context.Context, handler inmem.LoadHandler) error {
	return s.BackingStore.Load(ctx, func(typ resource.Type, value resource.Resource) error {
		if typ != SelectionType || value.Metadata().Type() != SelectionType || value.Metadata().ID() != ResourceID {
			return fmt.Errorf("unexpected persisted bootstrap resource %s/%s", typ, value.Metadata().ID())
		}
		return handler(typ, value)
	})
}

func (s selectionBackingStore) Put(ctx context.Context, typ resource.Type, value resource.Resource) error {
	if typ != SelectionType {
		return nil
	}
	return s.BackingStore.Put(ctx, typ, value)
}

func (s selectionBackingStore) Destroy(ctx context.Context, typ resource.Type, ptr resource.Pointer) error {
	if typ != SelectionType {
		return nil
	}
	return s.BackingStore.Destroy(ctx, typ, ptr)
}

// Selection is a singleton and never updated. Reconstructing its fixed COSI
// metadata avoids a second schema for a serialized resource envelope.
type selectionMarshaler struct{}

type selectionDisk struct {
	Script []byte `json:"script"`
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
