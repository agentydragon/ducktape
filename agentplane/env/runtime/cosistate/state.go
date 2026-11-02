// Package cosistate configures selective persistence for Runtime COSI resources.
package cosistate

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/cosi-project/runtime/pkg/state/impl/store/bolt"
	"go.etcd.io/bbolt"
)

type Codec struct {
	Type      resource.Type
	Marshal   func(resource.Resource) ([]byte, error)
	Unmarshal func([]byte) (resource.Resource, error)
}

type (
	marshaler struct{ codecs map[resource.Type]Codec }
	envelope  struct {
		Type resource.Type   `json:"type"`
		Data json.RawMessage `json:"data"`
	}
)

func (m marshaler) MarshalResource(value resource.Resource) ([]byte, error) {
	codec, ok := m.codecs[value.Metadata().Type()]
	if !ok {
		return nil, fmt.Errorf("unsupported persistent resource %s", value.Metadata().Type())
	}
	data, err := codec.Marshal(value)
	if err != nil {
		return nil, err
	}
	return json.Marshal(envelope{Type: codec.Type, Data: data})
}

func (m marshaler) UnmarshalResource(data []byte) (resource.Resource, error) {
	var disk envelope
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&disk); err != nil {
		return nil, err
	}
	var trailing json.RawMessage
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, errors.New("invalid trailing persisted resource")
	}
	codec, ok := m.codecs[disk.Type]
	if !ok {
		return nil, fmt.Errorf("unsupported persistent resource %s", disk.Type)
	}
	value, err := codec.Unmarshal(disk.Data)
	if err != nil {
		return nil, err
	}
	if value.Metadata().Type() != disk.Type {
		return nil, errors.New("persisted resource type mismatch")
	}
	return value, nil
}

type selectiveBacking struct {
	inmem.BackingStore
	codecs map[resource.Type]Codec
}

func (s selectiveBacking) Load(ctx context.Context, handler inmem.LoadHandler) error {
	return s.BackingStore.Load(ctx, func(typ resource.Type, value resource.Resource) error {
		if _, ok := s.codecs[typ]; !ok || value.Metadata().Type() != typ {
			return fmt.Errorf("unexpected persisted resource %s/%s", typ, value.Metadata().ID())
		}
		return handler(typ, value)
	})
}

func (s selectiveBacking) Put(ctx context.Context, typ resource.Type, value resource.Resource) error {
	if _, ok := s.codecs[typ]; !ok {
		return nil
	}
	return s.BackingStore.Put(ctx, typ, value)
}

func (s selectiveBacking) Destroy(ctx context.Context, typ resource.Type, ptr resource.Pointer) error {
	if _, ok := s.codecs[typ]; !ok {
		return nil
	}
	return s.BackingStore.Destroy(ctx, typ, ptr)
}

// Open persists only the registered resource types in namespace. Process and
// admission state remains in memory and is never restored on startup.
func Open(stateDir string, namespace resource.Namespace, registered ...Codec) (state.State, *bolt.BackingStore, error) {
	codecs := make(map[resource.Type]Codec, len(registered))
	for _, codec := range registered {
		if codec.Type == "" || codec.Marshal == nil || codec.Unmarshal == nil {
			return nil, nil, errors.New("invalid persistent resource codec")
		}
		if _, ok := codecs[codec.Type]; ok {
			return nil, nil, fmt.Errorf("duplicate persistent resource type %s", codec.Type)
		}
		codecs[codec.Type] = codec
	}
	backing, err := bolt.NewBackingStore(func() (*bbolt.DB, error) {
		return bbolt.Open(filepath.Join(stateDir, "runtime-resources.db"), 0o600, nil)
	}, marshaler{codecs: codecs})
	if err != nil {
		return nil, nil, err
	}
	directory, err := os.Open(stateDir)
	if err == nil {
		err = directory.Sync()
		err = errors.Join(err, directory.Close())
	}
	if err != nil {
		return nil, nil, errors.Join(err, backing.Close())
	}
	selected := selectiveBacking{BackingStore: backing.WithNamespace(namespace), codecs: codecs}
	build := func(ns resource.Namespace) state.CoreState {
		if ns == namespace {
			return inmem.NewStateWithOptions(inmem.WithBackingStore(selected))(ns)
		}
		return inmem.Build(ns)
	}
	return state.WrapCore(namespaced.NewState(build)), backing, nil
}
