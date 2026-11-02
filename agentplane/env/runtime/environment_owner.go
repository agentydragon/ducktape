package runtime

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

const environmentRuntimeOwnerName = ".agentplane-environment-runtime-owner"

// environmentOwner is the Pod/subprocess child-lifetime fence. Native children
// inherit its descriptor; the COSI backing database separately locks Runtime state.
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

func syncDirectory(path string) error {
	file, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open directory %s for sync: %w", path, err)
	}
	defer file.Close()
	return file.Sync()
}

// ensureDirectory syncs each parent after creating its child, matching the retained
// state directory durability contract.
func ensureDirectory(path string) error {
	abs, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	var missing []string
	parent := filepath.Clean(abs)
	for {
		_, err := os.Stat(parent)
		if err == nil {
			break
		}
		if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		missing = append(missing, parent)
		next := filepath.Dir(parent)
		if next == parent {
			return fmt.Errorf("no existing parent for %s", path)
		}
		parent = next
	}
	if err := syncDirectory(filepath.Dir(parent)); err != nil {
		return err
	}
	for i := len(missing) - 1; i >= 0; i-- {
		if err := os.Mkdir(missing[i], 0o777); err != nil && !errors.Is(err, os.ErrExist) {
			return err
		}
		if err := syncDirectory(filepath.Dir(missing[i])); err != nil {
			return err
		}
	}
	return nil
}

func (o *environmentOwner) Close() error {
	if o == nil || o.file == nil {
		return nil
	}
	file := o.file
	o.file = nil
	return errors.Join(syscall.Flock(int(file.Fd()), syscall.LOCK_UN), file.Close())
}
