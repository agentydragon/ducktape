package harnesshost

import (
	"context"
	"errors"
	"fmt"

	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
)

// The fixture supplies the same shared-engine boundary as Environment Runtime.
// Host cancellation stops this test-owned engine after the Host reaches Closed.
func newTestRunner(config Config, store state.State) (*Runner, error) {
	if store == nil {
		store = state.WrapCore(namespaced.NewState(inmem.Build))
	}
	engine, err := cosiruntime.NewRuntime(store, zap.L().Named("lifecycle"))
	if err != nil {
		return nil, err
	}
	engineDone := make(chan struct{})
	r, err := NewEmbedded(config, store, engine, engineDone)
	if err != nil {
		return nil, err
	}
	go func() {
		err := engine.Run(r.ctx)
		if r.ctx.Err() == nil {
			if err == nil {
				err = errors.New("Host lifecycle engine stopped unexpectedly")
			}
			err = fmt.Errorf("Host lifecycle engine stopped: %w", err)
			r.lifecycle.finish(err)
			r.fail(err)
		}
		close(engineDone)
	}()
	return r, nil
}

func newLifecycleWatchFixture(ctx context.Context) *lifecycle {
	return &lifecycle{runner: &Runner{ctx: ctx}, state: state.WrapCore(namespaced.NewState(inmem.Build))}
}
