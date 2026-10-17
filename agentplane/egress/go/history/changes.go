package history

import (
	"context"
	"fmt"
	"time"

	"github.com/lib/pq"
)

// ChangesHeartbeat is the quiet after which a change stream sends a frame anyway, so a reader whose
// connection died silently finds out by its own read timeout, which must be longer.
const ChangesHeartbeat = 15 * time.Second

// Changes tells a reader when decisions may have been committed, from any replica.
type Changes struct {
	dsn       string
	heartbeat time.Duration
}

// NewChanges follows the history database at `databaseURL`, with a frame at least every `heartbeat`.
func NewChanges(databaseURL string, heartbeat time.Duration) (*Changes, error) {
	dsn, err := connectionString(databaseURL, "agentplane-egress-decision-changes")
	if err != nil {
		return nil, err
	}
	return &Changes{dsn: dsn, heartbeat: heartbeat}, nil
}

// Follow calls `changed` once subscribed, so a reader re-reads what committed before it listened, then
// after each commit and after every heartbeat of quiet. It returns when `changed` fails, `ctx` ends, or
// the LISTEN connection is lost; the reader subscribes again.
//
// Each follower holds its own connection, opened here: the proxy's admission must not depend on a
// database that is down when it starts, and a follower finds that out on its own request. The
// connection is a pq.ListenerConn rather than a pq.Listener because a Listener reconnects behind the
// follower's back, and a stream that outlives its LISTEN would go on reporting quiet.
func (c *Changes) Follow(ctx context.Context, changed func() error) error {
	notifications := make(chan *pq.Notification, 32)
	conn, err := pq.NewListenerConn(c.dsn, notifications)
	if err != nil {
		return err
	}
	defer conn.Close()
	// Closing the connection ends the notifications, which wakes the loop below.
	defer context.AfterFunc(ctx, func() { conn.Close() })()
	if _, err := conn.Listen(decisionsChannel); err != nil {
		return err
	}
	for {
		if err := changed(); err != nil {
			return err
		}
		select {
		case _, open := <-notifications:
			if !open {
				if ctx.Err() != nil {
					return ctx.Err()
				}
				return fmt.Errorf("listen connection lost: %w", conn.Err())
			}
			// Commits that landed while the reader was being told share its next frame.
			for range len(notifications) {
				<-notifications
			}
		case <-time.After(c.heartbeat):
		}
	}
}
