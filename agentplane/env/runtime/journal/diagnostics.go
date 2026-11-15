package journal

import (
	"errors"
	"os"
	"syscall"

	"go.uber.org/zap"
	"modernc.org/sqlite"
)

func pathIdentity(path, prefix string) []zap.Field {
	info, err := os.Stat(path)
	if err != nil {
		return []zap.Field{zap.NamedError(prefix+"_stat_error", err)}
	}
	stat := info.Sys().(*syscall.Stat_t)
	return []zap.Field{zap.Uint64(prefix+"_device", uint64(stat.Dev)), zap.Uint64(prefix+"_inode", stat.Ino)}
}

func journalLogger(path, sourceID string) *zap.Logger {
	return zap.L().With(
		zap.String("source_id", sourceID), zap.String("journal_path", path), zap.Int("pid", os.Getpid()),
	).With(pathIdentity(path, "open_path")...)
}

// Preserve the first failure before later callers see only ErrFailed. Path stats
// are observations, not proof of which file descriptor or process held a lock.
func (j *Journal) recordFailureLocked(phase string, err error) {
	if j.failure == nil {
		if entry := j.logger.Check(zap.ErrorLevel, "Session journal failed"); entry != nil {
			fields := []zap.Field{zap.String("phase", phase), zap.Error(err)}
			var sqliteErr *sqlite.Error
			if errors.As(err, &sqliteErr) {
				fields = append(fields, zap.Int("sqlite_code", sqliteErr.Code()))
			}
			// The path may have been replaced since Open. Report that observation
			// separately from the identity recorded when this Journal opened.
			fields = append(fields, pathIdentity(j.path, "failure_path")...)
			entry.Write(fields...)
		}
		if entry := j.logger.Check(zap.DebugLevel, "Session journal failure caller"); entry != nil {
			entry.Write(zap.String("phase", phase), zap.Stack("stack"))
		}
	}
	j.failure = err
	j.notifyLocked()
}
