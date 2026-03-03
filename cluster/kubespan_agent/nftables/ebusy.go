package nftables

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"syscall"
	"time"

	"github.com/google/nftables"
	"go.uber.org/zap"
)

// DIVERGENCE: Talos relies on COSI runtime restart for EBUSY recovery, which
// restarts the entire controller. We add explicit exponential backoff with
// jitter around conn.Flush(), which is more responsive and avoids re-reading
// COSI state on every retry.

const (
	ebusyMaxTimeout = 30 * time.Second
	ebusyBaseDelay  = 50 * time.Millisecond
	ebusyMaxDelay   = 2 * time.Second
)

// flushWithEBUSYRetry retries conn.Flush() with exponential backoff when the
// kernel returns EBUSY (nf_tables_commit_mutex contention).
func flushWithEBUSYRetry(conn *nftables.Conn, logger *zap.Logger) error {
	deadline := time.Now().Add(ebusyMaxTimeout)
	delay := ebusyBaseDelay

	var lastErr error
	for attempt := 0; time.Now().Before(deadline); attempt++ {
		if attempt > 0 {
			jitter := time.Duration(rand.Int64N(int64(delay)))
			time.Sleep(delay + jitter)
			delay = min(delay*2, ebusyMaxDelay)
		}

		lastErr = conn.Flush()
		if lastErr == nil {
			if attempt > 0 {
				logger.Info("nftables flush succeeded after EBUSY retries", zap.Int("attempts", attempt+1))
			}
			return nil
		}

		if !errors.Is(lastErr, syscall.EBUSY) {
			return lastErr
		}

		if attempt == 0 || (attempt+1)%10 == 0 {
			logger.Debug("nftables EBUSY, retrying",
				zap.Int("attempt", attempt+1),
				zap.Duration("next_delay", delay),
			)
		}
	}

	return fmt.Errorf("nftables flush failed after retries: %w", lastErr)
}
