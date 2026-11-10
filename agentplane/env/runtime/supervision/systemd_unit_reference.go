package supervision

import (
	"context"
	"sync"

	"github.com/godbus/dbus/v5"
)

// systemdUnitReference pins a unit object until its process-domain fence is
// proven. A retained reference also keeps the API-bus connection used for
// systemd manager operations alive.
type systemdUnitReference interface {
	Connected() bool
	Close() error
}

type systemdReferenceBus interface {
	systemdUnitReference
	refUnit(context.Context, string) error
}

type dbusSystemdReferenceBus struct{ conn *dbus.Conn }

func (b dbusSystemdReferenceBus) Connected() bool { return b.conn.Connected() }

func (b dbusSystemdReferenceBus) Close() error { return b.conn.Close() }

func (b dbusSystemdReferenceBus) refUnit(ctx context.Context, name string) error {
	return b.conn.Object("org.freedesktop.systemd1", "/org/freedesktop/systemd1").CallWithContext(
		ctx, "org.freedesktop.systemd1.Manager.RefUnit", 0, name,
	).Err
}

type retainedSystemdUnitReference struct {
	bus       systemdReferenceBus
	cancel    context.CancelFunc
	closeOnce sync.Once
	closeErr  error
}

func (r *retainedSystemdUnitReference) Connected() bool {
	return r.bus.Connected()
}

func (r *retainedSystemdUnitReference) Close() error {
	r.closeOnce.Do(func() {
		r.closeErr = r.bus.Close()
		r.cancel()
	})
	return r.closeErr
}

func referenceSystemdUnit(ctx context.Context, name string) (systemdUnitReference, error) {
	// godbus.WithContext controls the connection lifetime, so keep it separate
	// from ctx, which bounds only the RefUnit RPC.
	return referenceSystemdUnitWith(ctx, name, func(lifetime context.Context) (systemdReferenceBus, error) {
		conn, err := dbus.ConnectSystemBus(dbus.WithContext(lifetime))
		if err != nil {
			return nil, err
		}
		return dbusSystemdReferenceBus{conn: conn}, nil
	})
}

func referenceSystemdUnitWith(ctx context.Context, name string, dial func(context.Context) (systemdReferenceBus, error)) (systemdUnitReference, error) {
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
	reference := &retainedSystemdUnitReference{bus: bus, cancel: cancel}
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
