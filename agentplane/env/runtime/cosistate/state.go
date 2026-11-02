// Package cosistate configures selective persistence for Runtime COSI resources.
package cosistate

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/cosi-project/runtime/pkg/state/impl/store"
	"github.com/cosi-project/runtime/pkg/state/impl/store/bolt"
	"go.etcd.io/bbolt"
)

// Registration names a durable type and checks its domain invariants on both
// writes and reload. COSI's protobuf registry owns the wire representation.
type Registration struct {
	Type     resource.Type
	Validate func(resource.Resource) error
}

type selectiveBacking struct {
	inmem.BackingStore
	registered map[resource.Type]Registration
}

func (s selectiveBacking) Load(ctx context.Context, handler inmem.LoadHandler) error {
	return s.BackingStore.Load(ctx, func(typ resource.Type, value resource.Resource) error {
		registration, ok := s.registered[typ]
		if !ok || value.Metadata().Type() != typ {
			return fmt.Errorf("unexpected persisted resource %s/%s", typ, value.Metadata().ID())
		}
		if err := registration.Validate(value); err != nil {
			return fmt.Errorf("invalid persisted %s/%s: %w", typ, value.Metadata().ID(), err)
		}
		return handler(typ, value)
	})
}

func (s selectiveBacking) Put(ctx context.Context, typ resource.Type, value resource.Resource) error {
	registration, ok := s.registered[typ]
	if !ok {
		return nil
	}
	if err := registration.Validate(value); err != nil {
		return err
	}
	return s.BackingStore.Put(ctx, typ, value)
}

func (s selectiveBacking) Destroy(ctx context.Context, typ resource.Type, ptr resource.Pointer) error {
	if _, ok := s.registered[typ]; !ok {
		return nil
	}
	return s.BackingStore.Destroy(ctx, typ, ptr)
}

// Open persists only the registered resource types in namespace. Process and
// admission state remains in memory and is never restored on startup.
func Open(stateDir string, namespace resource.Namespace, registered ...Registration) (state.State, *bolt.BackingStore, error) {
	selectedTypes := make(map[resource.Type]Registration, len(registered))
	for _, registration := range registered {
		if registration.Type == "" || registration.Validate == nil {
			return nil, nil, errors.New("invalid persistent resource registration")
		}
		if _, ok := selectedTypes[registration.Type]; ok {
			return nil, nil, fmt.Errorf("duplicate persistent resource type %s", registration.Type)
		}
		selectedTypes[registration.Type] = registration
	}
	backing, err := bolt.NewBackingStore(func() (*bbolt.DB, error) {
		// Fail promptly if another Runtime owns this store rather than blocking
		// startup indefinitely. bbolt takes an exclusive lock for its lifetime.
		return bbolt.Open(filepath.Join(stateDir, "runtime-resources.db"), 0o600, &bbolt.Options{Timeout: time.Millisecond})
	}, store.ProtobufMarshaler{})
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
	selected := selectiveBacking{BackingStore: backing.WithNamespace(namespace), registered: selectedTypes}
	build := func(ns resource.Namespace) state.CoreState {
		if ns == namespace {
			return inmem.NewStateWithOptions(inmem.WithBackingStore(selected))(ns)
		}
		return inmem.Build(ns)
	}
	return state.WrapCore(namespaced.NewState(build)), backing, nil
}
