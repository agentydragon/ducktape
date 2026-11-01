package runtime

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

const environmentRuntimeOwnerName = ".agentplane-environment-runtime-owner"

// environmentOwner holds the Runtime lifetime fence. Its nonblocking flock prevents a second
// Environment Runtime from taking ownership of the same Sandbox state concurrently.
type environmentOwner struct {
	file *os.File
}

func openEnvironmentOwner(stateDir string) (*environmentOwner, error) {
	if err := ensureDirectory(stateDir); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(filepath.Join(stateDir, environmentRuntimeOwnerName), os.O_RDONLY|os.O_CREATE, 0o600)
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
