package startup

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// Publish writes one complete startup value for a parent process to discover atomically.
func Publish(path string, value any) error {
	if path == "" {
		return nil
	}
	contents, err := json.Marshal(value)
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".startup-*")
	if err != nil {
		return fmt.Errorf("create startup endpoint file: %w", err)
	}
	defer os.Remove(temporary.Name())
	if _, err := temporary.Write(contents); err != nil {
		_ = temporary.Close()
		return fmt.Errorf("write startup endpoints: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return fmt.Errorf("sync startup endpoints: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close startup endpoints: %w", err)
	}
	if err := os.Rename(temporary.Name(), path); err != nil {
		return fmt.Errorf("publish startup endpoints: %w", err)
	}
	return nil
}
