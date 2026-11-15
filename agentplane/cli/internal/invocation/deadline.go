package invocation

import (
	"context"
	"errors"
	"time"
)

// deadlineElapsed recognizes the RPC deadline boundary even if the context timer has not yet
// published DeadlineExceeded to Err. grpc-go may surface that deadline just before ctx.Err flips.
func DeadlineElapsed(ctx context.Context) bool {
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return true
	}
	deadline, bounded := ctx.Deadline()
	return bounded && !time.Now().Before(deadline)
}
