package supervision

import (
	"context"
	"errors"
	"sync"
	"time"

	systemd "github.com/coreos/go-systemd/v22/dbus"
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
	manager   *systemd.Conn
	cancel    context.CancelFunc
	closeOnce sync.Once
	closeErr  error
}

func (r *retainedSystemdUnitReference) Connected() bool {
	if r.manager != nil {
		return r.manager.Connected()
	}
	return r.bus.Connected()
}

func (r *retainedSystemdUnitReference) Close() error {
	r.closeOnce.Do(func() {
		if r.manager != nil {
			r.manager.Close()
		} else {
			r.closeErr = r.bus.Close()
		}
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
	return acquireSystemdUnitReference(ctx, name, func(lifetime context.Context) (*systemd.Conn, systemdReferenceBus, error) {
		bus, err := dial(lifetime)
		return nil, bus, err
	})
}

// SystemdUnitReference holds a systemd manager connection and a RefUnit pin
// through the exact invocation's process-domain fence.
type SystemdUnitReference struct {
	reference *retainedSystemdUnitReference
}

// OpenSystemdUnitReference connects through the API bus, pins name before the
// caller can start it, and returns the same manager connection for all later
// operations. The setup/RPC context does not own the retained connection.
func OpenSystemdUnitReference(ctx context.Context, name string) (*SystemdUnitReference, error) {
	setupCtx, cancelSetup := context.WithTimeout(ctx, 30*time.Second)
	defer cancelSetup()
	reference, err := openSystemdUnitManagerReferenceWith(setupCtx, name, dialSystemdAPIManager)
	if err != nil {
		return nil, err
	}
	return &SystemdUnitReference{reference: reference}, nil
}

func openSystemdUnitManagerReferenceWith(ctx context.Context, name string, dial func(context.Context) (*systemd.Conn, systemdReferenceBus, error)) (*retainedSystemdUnitReference, error) {
	return acquireSystemdUnitReference(ctx, name, dial)
}

func acquireSystemdUnitReference(ctx context.Context, name string, dial func(context.Context) (*systemd.Conn, systemdReferenceBus, error)) (*retainedSystemdUnitReference, error) {
	lifetime, cancel := context.WithCancel(context.Background())
	stopSetupCancel := context.AfterFunc(ctx, cancel)
	detached := false
	defer func() {
		if !detached {
			stopSetupCancel()
			cancel()
		}
	}()
	manager, bus, err := dial(lifetime)
	if err == nil {
		err = lifetime.Err()
	}
	if err != nil {
		if manager != nil {
			manager.Close()
		} else if bus != nil {
			_ = bus.Close()
		}
		return nil, err
	}
	reference := &retainedSystemdUnitReference{manager: manager, bus: bus, cancel: cancel}
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

func dialSystemdAPIManager(lifetime context.Context) (*systemd.Conn, systemdReferenceBus, error) {
	var managerBus *dbus.Conn
	var dials int
	manager, err := systemd.NewConnection(func() (*dbus.Conn, error) {
		bus, err := dbus.ConnectSystemBus(dbus.WithContext(lifetime))
		if err == nil && dials == 0 {
			managerBus = bus
		}
		dials++
		return bus, err
	})
	if err != nil {
		return nil, nil, err
	}
	if dials < 2 || managerBus == nil {
		manager.Close()
		return nil, nil, errors.New("systemd manager connection did not establish its API-bus peers")
	}
	if err := manager.Subscribe(); err != nil {
		manager.Close()
		return nil, nil, err
	}
	return manager, dbusSystemdReferenceBus{conn: managerBus}, nil
}

// Conn returns the API-bus manager connection protected by this unit reference.
func (r *SystemdUnitReference) Conn() *systemd.Conn {
	if r == nil || r.reference == nil {
		return nil
	}
	return r.reference.manager
}

// Close releases the connection and reference. Callers must first prove the
// invocation's process domain is empty.
func (r *SystemdUnitReference) Close() error {
	if r == nil || r.reference == nil {
		return nil
	}
	return r.reference.Close()
}
