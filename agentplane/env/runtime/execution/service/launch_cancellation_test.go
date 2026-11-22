package service

import (
	"context"
	"errors"
	"testing"
	"testing/synctest"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/backend"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
)

func TestCancellationDuringStartOwnedRequiresJoinedFenceProof(t *testing.T) {
	fenceErr := errors.New("native process fence failed")
	for _, test := range []struct {
		name      string
		launchErr error
		clean     bool
	}{
		{name: "joined fenced launch", launchErr: errors.Join(supervision.ErrFencedLaunch, context.Canceled), clean: true},
		{name: "unproven cancellation", launchErr: context.Canceled},
		{name: "failed fence", launchErr: errors.Join(context.Canceled, fenceErr)},
	} {
		t.Run(test.name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				ctx, cancel := context.WithCancel(t.Context())
				defer cancel()
				entered, canceled, releaseFence := make(chan struct{}), make(chan struct{}), make(chan struct{})
				start := backend.StartOwned(backend.StartConfig{
					Binary: "codex", Config: backend.Config{URL: "ws://127.0.0.1:4501", Owner: backend.OwnerSystemd, Placement: backend.PlacementDirect},
					LaunchProcess: func(ctx context.Context, _ supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
						close(entered)
						<-ctx.Done()
						close(canceled)
						<-releaseFence
						return nil, test.launchErr
					},
				})
				finished := make(chan error, 1)
				go func() {
					finished <- runWithReady(ctx, Config{}, start, func(executorpb.ExecutorServer, context.Context) { t.Error("interrupted launch published an executor") })
				}()
				<-entered
				cancel()
				<-canceled
				synctest.Wait()
				select {
				case err := <-finished:
					t.Fatalf("service returned before native fence settled: %v", err)
				default:
				}
				close(releaseFence)
				err := <-finished
				if test.clean {
					if err != nil {
						t.Fatalf("joined launch fence failed: %v", err)
					}
				} else if !errors.Is(err, test.launchErr) {
					t.Fatalf("service lost unproven launch error: %v", err)
				}
			})
		})
	}
}
