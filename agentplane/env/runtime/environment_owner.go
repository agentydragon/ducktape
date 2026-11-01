package runtime

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

const legacyOwnerName = ".agentplane-runner-owner"

// environmentOwner retains the original Runner ownership inode across the service split. The
// filename and nonblocking flock are a deployment fence: a new Runtime cannot acquire state while
// a legacy Runner still owns it, and a stale queued native launch cannot cross into a successor.
type environmentOwner struct {
	file *os.File
}

func openEnvironmentOwner(stateDir string) (*environmentOwner, error) {
	if err := ensureDirectory(stateDir); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(filepath.Join(stateDir, legacyOwnerName), os.O_RDONLY|os.O_CREATE, 0o600)
	if err != nil {
		return nil, fmt.Errorf("cannot fence environment state directory %s: %w", stateDir, err)
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, fmt.Errorf("environment state directory %s is already owned", stateDir)
		}
		return nil, fmt.Errorf("cannot fence environment state directory %s: %w", stateDir, err)
	}
	return &environmentOwner{file: file}, nil
}

func (o *environmentOwner) Close() error {
	if o == nil || o.file == nil {
		return nil
	}
	file := o.file
	o.file = nil
	return errors.Join(syscall.Flock(int(file.Fd()), syscall.LOCK_UN), file.Close())
}
