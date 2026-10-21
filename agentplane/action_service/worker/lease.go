package worker

import (
	"context"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
)

// Lease is an Executor's hold on the one Execution it runs: the lease its claim granted.
type Lease struct {
	store    *store.Store
	claim    store.ExecutionClaim
	duration time.Duration
}

// RenewalInterval is how often Hold renews the lease and how long one renewal may take: a third of its window, so a
// renewal that fails leaves time for another before the lease lapses.
func (l *Lease) RenewalInterval() time.Duration { return l.duration / 3 }

// renew extends the lease by its window. False means this executor no longer owns the attempt.
func (l *Lease) renew(ctx context.Context) (bool, error) {
	ctx, cancel := context.WithTimeout(ctx, l.RenewalInterval())
	defer cancel()
	return l.store.HeartbeatExecution(ctx, l.claim.RequestID, l.claim.Lease, l.duration)
}

// Renew proves this executor still owns the attempt. It fails with ErrOutcomeUnknown once the lease is no longer
// this executor's, or when renewal does not succeed in time: the effect may be under way, so the attempt stops
// waiting for it and is never retried.
func (l *Lease) Renew(ctx context.Context) error {
	owned, err := l.renew(ctx)
	if err != nil {
		// Database errors can carry request material; the type names the failure.
		return fmt.Errorf("%w: lease renewal failed (%T)", ErrOutcomeUnknown, err)
	}
	if !owned {
		return fmt.Errorf("%w: lease lost", ErrOutcomeUnknown)
	}
	return nil
}

// Hold runs work while renewing lease every RenewalInterval, so an attempt that outlives one lease window is not
// swept out from under it. It renews before work starts. When a renewal fails before work returns, Hold ends work's
// context, waits for it, and returns the renewal's ErrOutcomeUnknown.
//
// Renewal proves the executor alive, not that the backend progresses: the caller bounds work, or a wedged call is
// renewed for ever.
func Hold[T any](ctx context.Context, lease *Lease, work func(context.Context) (T, error)) (T, error) {
	var none T
	if err := lease.Renew(ctx); err != nil {
		return none, err
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	type result struct {
		value T
		err   error
	}
	done := make(chan result, 1)
	go func() {
		value, err := work(ctx)
		done <- result{value, err}
	}()
	timer := time.NewTimer(lease.RenewalInterval())
	defer timer.Stop()
	for {
		select {
		case finished := <-done:
			return finished.value, finished.err
		case <-timer.C:
		}
		if err := lease.Renew(ctx); err != nil {
			select {
			case finished := <-done:
				return finished.value, finished.err
			default:
			}
			cancel()
			<-done
			return none, err
		}
		timer.Reset(lease.RenewalInterval())
	}
}
