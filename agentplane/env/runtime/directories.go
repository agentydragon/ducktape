package runtime

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

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
