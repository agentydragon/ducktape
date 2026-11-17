package systemd

import (
	"context"
	"sync"

	"github.com/godbus/dbus/v5"
)

// UnitReference pins a unit object until its process-domain fence is
// proven. A retained reference also keeps the API-bus connection used for
// systemd manager operations alive.
type UnitReference interface {
	Connected() bool
	Close() error
}

type referenceBus interface {
	UnitReference
	refUnit(context.Context, string) error
}

type dbusReferenceBus struct{ conn *dbus.Conn }

func (b dbusReferenceBus) Connected() bool { return b.conn.Connected() }

func (b dbusReferenceBus) Close() error { return b.conn.Close() }

func (b dbusReferenceBus) refUnit(ctx context.Context, name string) error {
	return b.conn.Object("org.freedesktop.systemd1", "/org/freedesktop/systemd1").CallWithContext(
		ctx, "org.freedesktop.systemd1.Manager.RefUnit", 0, name,
	).Err
}

type retainedUnitReference struct {
	bus       referenceBus
	cancel    context.CancelFunc
	closeOnce sync.Once
	closeErr  error
}

func (r *retainedUnitReference) Connected() bool {
	return r.bus.Connected()
}

func (r *retainedUnitReference) Close() error {
	r.closeOnce.Do(func() {
		r.closeErr = r.bus.Close()
		r.cancel()
	})
	return r.closeErr
}

func ReferenceUnit(ctx context.Context, name string) (UnitReference, error) {
	// godbus.WithContext controls the connection lifetime, so keep it separate
	// from ctx, which bounds only the RefUnit RPC.
	return referenceUnitWith(ctx, name, func(lifetime context.Context) (referenceBus, error) {
		conn, err := dbus.ConnectSystemBus(dbus.WithContext(lifetime))
		if err != nil {
			return nil, err
		}
		return dbusReferenceBus{conn: conn}, nil
	})
}

func referenceUnitWith(ctx context.Context, name string, dial func(context.Context) (referenceBus, error)) (UnitReference, error) {
	lifetime, cancel := context.WithCancel(context.Background())
	stopSetupCancel := context.AfterFunc(ctx, cancel)
	detached := false
	defer func() {
		if !detached {
			stopSetupCancel()
			cancel()
		}
	}()
	bus, err := dial(lifetime)
	if err == nil {
		err = lifetime.Err()
	}
	if err != nil {
		if bus != nil {
			_ = bus.Close()
		}
		return nil, err
	}
	reference := &retainedUnitReference{bus: bus, cancel: cancel}
	if err := bus.refUnit(ctx, name); err != nil {
		_ = reference.Close()
		return nil, err
	}
	if !stopSetupCancel() {
		_ = reference.Close()
		detached = true
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		return nil, context.Canceled
	}
	detached = true
	return reference, nil
}
