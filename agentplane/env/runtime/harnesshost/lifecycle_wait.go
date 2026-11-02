package harnesshost

import "context"

// Subscribe before checking so a completion cannot be lost between the state
// read and the wait. This waits for observations; it never schedules effects.
func (l *lifecycle) waitFor(ctx context.Context, predicate func() (bool, error)) error {
	for {
		changed := l.host.changesSince()
		ready, err := predicate()
		if ready || err != nil {
			return err
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		case <-l.host.ctx.Done():
			return l.host.ctx.Err()
		case <-l.done.Done():
			return context.Cause(l.done)
		}
	}
}
