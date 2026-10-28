package runner

import "context"

// Subscribe before checking so a completion cannot be lost between the state
// read and the wait. This waits for observations; it never schedules effects.
func (l *lifecycle) waitFor(ctx context.Context, predicate func() (bool, error)) error {
	for {
		changed := l.runner.changesSince()
		ready, err := predicate()
		if ready || err != nil {
			return err
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		case <-l.runner.ctx.Done():
			return l.runner.ctx.Err()
		}
	}
}
