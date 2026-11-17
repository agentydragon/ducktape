package systemd

import (
	"context"
	"errors"
	"testing"
)

type fakeReferenceBus struct {
	connected bool
	closed    bool
	rpcCtx    context.Context
}

func (b *fakeReferenceBus) Connected() bool { return b.connected && !b.closed }

func (b *fakeReferenceBus) Close() error {
	b.closed = true
	return nil
}

func (b *fakeReferenceBus) refUnit(ctx context.Context, _ string) error {
	b.rpcCtx = ctx
	return nil
}

func TestUnitReferenceOutlivesSetupContext(t *testing.T) {
	setupCtx, cancelSetup := context.WithCancel(context.Background())
	bus := &fakeReferenceBus{connected: true}
	var lifetime context.Context
	reference, err := referenceUnitWith(setupCtx, "fixture.service", func(life context.Context) (referenceBus, error) {
		lifetime = life
		return bus, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	cancelSetup()
	if bus.rpcCtx == nil || bus.rpcCtx.Err() == nil {
		t.Fatalf("RefUnit RPC context after setup cancellation = %v, want canceled", bus.rpcCtx)
	}
	if lifetime == nil || lifetime.Err() != nil || !bus.Connected() || bus.closed {
		t.Fatalf("retained reference lifetime=%v connected=%t closed=%t; want live until intentional release", lifetime, bus.Connected(), bus.closed)
	}
	if err := reference.Close(); err != nil {
		t.Fatal(err)
	}
	if lifetime.Err() == nil || bus.Connected() || !bus.closed {
		t.Fatalf("after intentional release: lifetime=%v connected=%t closed=%t; want canceled and closed", lifetime.Err(), bus.Connected(), bus.closed)
	}
}

func TestUnitReferenceSetupCancellationStopsDial(t *testing.T) {
	setupCtx, cancelSetup := context.WithCancel(context.Background())
	dialStarted := make(chan struct{})
	dialDone := make(chan error, 1)
	go func() {
		_, err := referenceUnitWith(setupCtx, "fixture.service", func(life context.Context) (referenceBus, error) {
			close(dialStarted)
			<-life.Done()
			return nil, life.Err()
		})
		dialDone <- err
	}()
	<-dialStarted
	cancelSetup()
	if err := <-dialDone; !errors.Is(err, context.Canceled) {
		t.Fatalf("open after setup cancellation = %v, want context.Canceled", err)
	}
}
