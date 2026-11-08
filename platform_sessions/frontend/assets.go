// Package frontend contains the platform sessions owner UI.
package frontend

import (
	"fmt"
	"os"
	"path/filepath"
)

// Load reads the generated SPA files from its Bazel runfiles directory or image layer.
func Load(directory string) (map[string][]byte, error) {
	assets := make(map[string][]byte, 3)
	for _, name := range []string{"status.html", "main.css", "main.js"} {
		content, err := os.ReadFile(filepath.Join(directory, name))
		if err != nil {
			return nil, fmt.Errorf("read platform sessions frontend %q: %w", name, err)
		}
		assets[name] = content
	}
	return assets, nil
}
