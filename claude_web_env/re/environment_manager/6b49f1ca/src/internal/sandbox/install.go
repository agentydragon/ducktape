// Reconstructed from binary: environment-manager (Build ID 6b49f1ca, Go 1.25.6)
// Source: internal/sandbox/install.go
// Original path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/sandbox/install.go

package sandbox

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
)

// InstallSandboxRuntime installs or upgrades the sandbox runtime (srt) binary
// via npm. If version is "current", it logs and returns immediately (already
// up-to-date). Otherwise it attempts a global npm install of the
// @anthropic-ai/sandbox-runtime package.
//
// Parameters:
//   - logger: structured logger for the operation
//   - ctx: context for command execution
//   - version: the desired version string, or "current" to skip, or "" (defaults to "latest")
//
// Returns an error interface (nil on success or when gracefully skipped).
//
// Binary address: 0x7db400
func InstallSandboxRuntime(logger *slog.Logger, ctx context.Context, version string) error {
	if version == "" {
		version = "latest"
	}

	// If version is "current", skip installation entirely.
	if version == "current" {
		logger.Debug("Skipping sandbox-runtime installation (version=current)")
		return nil
	}

	// Determine the binary name from env or default to "srt".
	binaryName := os.Getenv("SRT_BINARY_PATH")
	if binaryName == "" {
		binaryName = "srt"
	}

	// Check if the binary already exists on PATH.
	existingPath, lookErr := exec.LookPath(binaryName)
	if lookErr == nil {
		// Binary found; log and skip install.
		logger.Debug("Found existing sandbox-runtime installation",
			"path", existingPath,
		)
	} else {
		logger.Debug("No existing sandbox-runtime installation found")
	}

	// Build the npm package specifier: @anthropic-ai/sandbox-runtime@<version>
	packageSpec := fmt.Sprintf("%s@%s", "@anthropic-ai/sandbox-runtime", version)

	// Log the install operation.
	logger.Info("Installing sandbox-runtime via npm",
		"package", packageSpec,
		"version", version,
	)

	// Run: npm install -g <packageSpec>
	cmd := exec.CommandContext(ctx, "npm", "install", "-g", packageSpec)
	output, err := cmd.CombinedOutput()

	if err != nil {
		if lookErr != nil {
			// npm install failed and we had no existing binary either.
			return fmt.Errorf("npm install failed: %w, output: %s", err, string(output))
		}

		// npm install failed but we had an existing binary; log warning and continue.
		logger.Warn("sandbox-runtime upgrade failed, continuing with existing installation",
			"error", string(output),
			"output", string(output),
		)
		return nil
	}

	// Install succeeded. Log the output.
	logger.Debug("npm install completed",
		"output", string(output),
	)

	// Verify the binary is now available.
	newPath, verifyErr := exec.LookPath(binaryName)
	if verifyErr != nil {
		if lookErr != nil {
			// Binary still not found and wasn't found before either.
			return fmt.Errorf("srt binary not found after installation: %w", verifyErr)
		}

		// Not found at new path but we had an existing installation.
		logger.Warn("srt not found after install, using existing installation",
			"error", verifyErr,
		)
		return nil
	}

	logger.Info("sandbox-runtime installed successfully",
		"version", version,
		"path", newPath,
	)

	return nil
}
