package util

import (
	"fmt"
	"log/slog"
	"os"
	"syscall"
)

// Reconstructed from symbol: internal/util.getLockPath
func getLockPath(sessionID string) string {
	dir := os.Getenv("TMPDIR")
	if dir == "" {
		dir = "/tmp"
	}
	return fmt.Sprintf("%s/environment-manager-%s.lock", dir, sessionID)
}

// Reconstructed from symbol: internal/util.AcquireLock
//
// AcquireLock acquires an flock-based file lock for the given session.
// It returns a cleanup function that releases the lock, or an error.
// If the lock is already held (EWOULDBLOCK), it logs a warning and returns
// a descriptive error indicating the session is already running.
func AcquireLock(ctx interface{ Value(any) any }, sessionName string, sessionID string) (func(), error) {
	lockPath := getLockPath(sessionID)

	file, err := os.OpenFile(lockPath, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("failed to open lockfile: %w", err)
	}

	err = syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	if err != nil {
		file.Close()

		if isEWOULDBLOCK(err) {
			slog.Warn("Session already running (lock held)",
				slog.String("session_id", sessionID),
				slog.String("session_id", sessionName),
				slog.String("lockfile_path", lockPath),
			)
			return nil, fmt.Errorf("session %s already running (lock held)", sessionName)
		}

		return nil, fmt.Errorf("failed to acquire lock: %w", err)
	}

	pid := os.Getpid()

	slog.Debug("Acquired lockfile using flock",
		slog.String("session_id", sessionID),
		slog.String("session_id", sessionName),
		slog.Int("pid", pid),
		slog.String("lockfile_path", lockPath),
	)

	// Reconstructed from symbol: internal/util.AcquireLock.func1
	cleanup := func() {
		if err := syscall.Flock(int(file.Fd()), syscall.LOCK_UN); err != nil {
			slog.Warn("Failed to unlock file", slog.String("error", err.Error()))
		}

		if err := file.Close(); err != nil {
			slog.Warn("Failed to close lockfile during cleanup", slog.String("error", err.Error()))
		}

		if err := os.Remove(lockPath); err != nil {
			slog.Warn("Could not remove lockfile", slog.String("error", err.Error()))
		}

		slog.Debug("Released lockfile",
			slog.String("session_id", sessionID),
			slog.String("lockfile_path", lockPath),
		)
	}

	return cleanup, nil
}

// isEWOULDBLOCK checks if the error is EWOULDBLOCK (syscall.EWOULDBLOCK).
func isEWOULDBLOCK(err error) bool {
	return err == syscall.EWOULDBLOCK
}

// containerLockPath is the fixed path for the container-level lock file.
const containerLockPath = "/tmp/environment-manager-container.lock"

// Reconstructed from symbol: internal/util.AcquireContainerLock
//
// AcquireContainerLock acquires a container-level flock to prevent multiple
// orchestrator/session instances from running concurrently on the same container.
// It returns a cleanup function that releases the lock, or an error.
func AcquireContainerLock(ctx interface{ Value(any) any }, sessionName string, sessionID string) (func(), error) {
	file, err := os.OpenFile(containerLockPath, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("failed to open container lockfile: %w", err)
	}

	err = syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	if err != nil {
		file.Close()

		if isEWOULDBLOCK(err) {
			slog.Warn("Another orchestrator/session is already running on this container",
				slog.String("session_id", sessionID),
				slog.String("lockfile_path", containerLockPath),
			)
			return nil, fmt.Errorf("container is already in use by another orchestrator/session (lock held at %s)", containerLockPath)
		}

		return nil, fmt.Errorf("failed to acquire container lock: %w", err)
	}

	slog.Debug("Acquired container-level lock",
		slog.String("session_id", sessionID),
		slog.String("lockfile_path", containerLockPath),
	)

	// Reconstructed from symbol: internal/util.AcquireContainerLock.func1
	cleanup := func() {
		if err := syscall.Flock(int(file.Fd()), syscall.LOCK_UN); err != nil {
			slog.Warn("Failed to unlock container lockfile", slog.String("error", err.Error()))
		}

		if err := file.Close(); err != nil {
			slog.Warn("Failed to close container lockfile during cleanup", slog.String("error", err.Error()))
		}

		if err := os.Remove(containerLockPath); err != nil {
			slog.Warn("Could not remove container lockfile", slog.String("error", err.Error()))
		}

		slog.Debug("Released container-level lock",
			slog.String("session_id", sessionID),
			slog.String("lockfile_path", containerLockPath),
		)
	}

	return cleanup, nil
}
