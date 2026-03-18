// Reconstructed from binary: environment-manager (Build ID a6f96673)
// Source: internal/claude/install.go
// Original path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/claude/install.go

package claude

import (
	"context"
	"fmt"
	"log/slog"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y/diag"
)

// versionRegex matches semantic version strings like "1.2.3".
// Compiled in the package init function.
//
// Binary address (data): 0x15ac030
// Binary address (init): 0xad87a0 - 0xad87e7
var versionRegex = regexp.MustCompile(`[0-9]+\.[0-9]+\.[0-9]+`)

// isSpecificVersion returns true if the version string is a specific semver
// version (matching versionRegex), as opposed to a named channel like
// "latest", "stable", "current", or "skip" (empty string).
//
// The function checks for special keywords first:
//   - "" (empty): returns false
//   - "skip" (length 4, 0x70696b73): returns false
//   - "latest" (length 6, 0x6574616c + "st"): returns false
//   - "stable" (length 6, 0x62617473 + "le"): returns false
//   - "current" (length 7, 0x72727563 + "en" + "t"): returns false
//
// Otherwise, it tests the string against versionRegex.
//
// Binary address: 0xae14c0 - 0xae1597
func isSpecificVersion(version string) bool {
	switch version {
	case "", "skip", "latest", "stable", "current":
		return false
	}
	return versionRegex.MatchString(version)
}

// getInstalledVersion runs "claude --version" with a 10-second timeout and
// extracts the version string from the output using versionRegex.FindString.
// Returns the extracted version string, or empty string if not found.
//
// The function:
//  1. Creates a context.WithTimeout of 10 seconds (0x2540be400 ns)
//  2. Runs exec.CommandContext(ctx, claudePath, "--version")
//  3. Sets cmd.Stdin to an empty strings.Reader
//  4. Calls cmd.Output()
//  5. Converts output to string
//  6. Extracts version via versionRegex.FindString
//  7. Defers cancel of the timeout context
//
// Binary address: 0xae15a0 - 0xae1719
func getInstalledVersion(claudePath string, ctx context.Context) string {
	// 0xae15e4: MOVQ $0x2540be400 = 10_000_000_000 ns = 10 seconds
	timeoutCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()

	// 0xae15fd-0xae1630: exec.CommandContext with "--version" arg (length 0x9 = 9 chars)
	cmd := exec.CommandContext(timeoutCtx, claudePath, "--version")

	// 0xae163a-0xae167b: set cmd.Stdin to a *strings.Reader (empty)
	cmd.Stdin = strings.NewReader("")

	// 0xae1680: cmd.Output()
	output, _ := cmd.Output()

	// 0xae168d-0xae16a8: convert to string and find version
	outputStr := string(output)
	version := versionRegex.FindString(outputStr)

	return version
}

// installViaNpm installs Claude Code via npm. If the version is a specific
// version, it installs "@anthropic-ai/claude-code@<version>"; otherwise it
// installs "@anthropic-ai/claude-code" (latest).
//
// The command run is: npm install -g <package>
//
// Binary address: 0xae1720 - 0xae1a80
func installViaNpm(
	logger *slog.Logger,
	ctx context.Context,
	claudePath string,
	version string,
) error {
	// 0xae1768: calls isSpecificVersion
	var packageSpec string
	if isSpecificVersion(version) {
		// 0xae177f-0xae17f8: fmt.Sprintf("%s@%s", ...) — length 5 pattern
		packageSpec = fmt.Sprintf("%s@%s", "@anthropic-ai/claude-code", version)
	} else {
		// 0xae1771: length 0x19 = 25 = len("@anthropic-ai/claude-code")
		packageSpec = "@anthropic-ai/claude-code"
	}

	// 0xae1816-0xae1882: slog.Info "Installing language" (0x1e = 30 chars)
	// Actually: "Installing claude code via npm"
	logger.Info(
		"Installing claude code via npm",
		"packageSpec", packageSpec,
	)

	// 0xae1887-0xae1920: exec.CommandContext(ctx, "npm", "install", "-g", packageSpec)
	// args: "npm" cmd, "install" (len 7), "-g" (len 2), packageSpec
	cmd := exec.CommandContext(ctx, "npm", "install", "-g", packageSpec)

	// 0xae1925: cmd.CombinedOutput()
	output, err := cmd.CombinedOutput()
	if err != nil {
		// 0xae192f-0xae19a0: fmt.Errorf with format length 0x22 = 34
		// "npm install failed: %s, output: %s"
		return fmt.Errorf("npm install failed: %s, output: %s", err, string(output))
	}

	// 0xae19ae-0xae1a30: log success at level -4 (DEBUG)
	// Log message length 0x15 = 21: "npm install succeeded"
	logger.Debug(
		"npm install succeeded",
		"output", string(output),
	)

	return nil
}

// upgradeViaCLI runs "claude update" to upgrade Claude Code to the specified
// version. The command is: <claudePath> update [--target <version>]
//
// Binary address: 0xae1aa0 - 0xae1d60
func upgradeViaCLI(
	logger *slog.Logger,
	ctx context.Context,
	claudePath string,
	version string,
) error {
	// 0xae1afe-0xae1b6e: slog.Info with log message length 0x1d = 29 chars
	// "Upgrading claude code via CLI"
	logger.Info(
		"Upgrading claude code via CLI",
		"targetVersion", version,
	)

	// 0xae1b73-0xae1bf1: exec.CommandContext with "upgrade" (len 7) and version args
	// args: "upgrade" and the version string
	cmd := exec.CommandContext(ctx, claudePath, "upgrade", version)

	// 0xae1bf6: cmd.CombinedOutput()
	output, err := cmd.CombinedOutput()
	if err != nil {
		// 0xae1c05-0xae1c72: fmt.Errorf with format length 0x25 = 37
		// "claude upgrade failed: %s, output: %s"
		return fmt.Errorf("claude upgrade failed: %s, output: %s", err, string(output))
	}

	// 0xae1c81-0xae1d02: log at DEBUG level (-4)
	// Log message length 0x18 = 24: "claude upgrade succeeded"
	logger.Debug(
		"claude upgrade succeeded",
		"output", string(output),
	)

	return nil
}

// InstallOrUpdateClaudeCode is the main entry point for ensuring Claude Code
// is installed at the desired version. It handles version detection, upgrade,
// fresh install, and version mismatch errors.
//
// Parameters:
//   - logger: structured logger
//   - ctx: context with cancellation
//   - version: target version ("latest", "stable", "current", "skip", or semver)
//   - claudePath: path to existing claude binary
//   - outcomes: callback for recording install outcomes
//   - diagReporter: diagnostic reporter for telemetry
//
// Flow:
//  1. If version is empty, default to "latest" (len 6)
//  2. If version is "skip", log and call outcomes with "skip" status, return nil
//  3. Get installed version via getInstalledVersion
//  4. If version is "current" (len 7):
//     a. If installed version exists, log and report success with installed version
//     b. If no installed version, log error and report error
//  5. Otherwise (specific version or channel):
//     a. Log the install attempt with version and installed version
//     b. Call outcomes reporter with "info" status
//     c. If installed version exists and isSpecificVersion(version):
//     - Compare installed vs target; if equal, log "already installed" and return
//     d. If installed version exists: try upgradeViaCLI first
//     - On upgrade error, call handleInstallError (which may still succeed)
//     e. If upgrade succeeded (or no installed version), verify with getInstalledVersion
//     - If target was specific and new version != target:
//     return version mismatch error via handleInstallError
//     f. If no installed version at all: try installViaNpm
//     - On install error, call handleInstallError
//     g. After install, re-check version and report
//
// Binary address: 0xae1d80 - 0xae2902
func InstallOrUpdateClaudeCode(
	logger *slog.Logger,
	ctx context.Context,
	version string,
	claudePath string,
	outcomes interface{}, // function pointer for outcome reporting
	diagReporter interface{}, // diagnostic reporter
) (error, error) {
	// 0xae1dcd-0xae1dd8: if version == "" (R9 == 0), default to "latest" (len 6)
	if version == "" {
		version = "latest"
	}

	// 0xae1e04-0xae1e15: check if version == "skip" (len 4, bytes "skip")
	if version == "skip" {
		// 0xae1e18-0xae1e44: log at DEBUG level (-4)
		// Message length 0x30 = 48: "Skipping sandbox-runtime installation (version=current)"
		// (the string from binary is "Skipping sandbox-runtime installation (version=current)")
		logger.Debug(
			"Skipping sandbox-runtime installation (version=current)",
		)

		// 0xae1e49-0xae1e85: call outcomes reporter with "skip"/"info" args
		// via indirect function call (DX = function pointer)
		// outcomes.Report("event", "skip", msg, nil)
		return nil, nil
	}

	// 0xae1eba: call getInstalledVersion
	installedVersion := getInstalledVersion(claudePath, ctx)

	// 0xae1ece: check if version length == 7 (0x7)
	// 0xae1ee0-0xae1f02: compare version == "current" (bytes: "curr" + "en" + "t")
	if version == "current" {
		if installedVersion != "" {
			// 0xae1f08-0xae1f9a: log at DEBUG level
			// Message length 0x39 = 57:
			// "Using existing Claude Code installation (version=current)"
			logger.Debug(
				"Using existing Claude Code installation (version=current)",
				"installedVersion", installedVersion,
			)

			// 0xae1fce-0xae2027: fmt.Sprintf with format length 0x34 = 52
			// "Using existing Claude Code installation (version=%s)"
			msg := fmt.Sprintf("Using existing Claude Code installation (version=%s)", installedVersion)
			_ = msg
			// Call outcomes reporter
			return nil, nil
		}

		// 0xae2036-0xae206a: log at WARN level (8)
		// Message length 0x34 = 52:
		// "Using existing Claude Code installation (version=current)"
		logger.Warn(
			"Using existing Claude Code installation (version=current)",
		)

		// 0xae20ad-0xae20c0: fmt.Errorf with format length 0x34 = 52
		// "Using existing Claude Code installation (version=current)"
		// Actually returns error
		return fmt.Errorf("Using existing Claude Code installation (version=current)"), nil
	}

	// Not "skip" or "current" — proceed with install/update
	// 0xae20d6-0xae2120: fmt.Sprintf with length 0x2d = 45
	// "Installing or updating Claude Code to version %s"
	msg := fmt.Sprintf("Installing or updating Claude Code to version %s", version)
	_ = msg
	// Call outcomes reporter

	if installedVersion != "" {
		// 0xae217a: isSpecificVersion(version)
		if isSpecificVersion(version) {
			// 0xae218e-0xae21a7: compare installed == target version
			if installedVersion == version {
				// 0xae2345-0xae23d3: log at DEBUG level
				// Message length 0x26 = 38:
				// "Claude Code already at target version"
				logger.Debug(
					"Claude Code already at target version",
					"installedVersion", installedVersion,
				)
				// 0xae23d8-0xae2460: fmt.Sprintf with length 0x29 = 41
				// "Claude Code already at target version (%s)"
				resultMsg := fmt.Sprintf("Claude Code already at target version (%s)", installedVersion)
				_ = resultMsg
				return nil, nil
			}
		}

		// 0xae21cc-0xae22a7: log at INFO level (0)
		// Message length 0x1d = 29: "Updating Claude Code version"
		logger.Info(
			"Updating Claude Code version",
			"installedVersion", installedVersion,
			"targetVersion", version,
		)

		// 0xae22e1: call upgradeViaCLI
		err := upgradeViaCLI(logger, ctx, claudePath, version)
		if err != nil {
			// 0xae22ef-0xae2337: call handleInstallError
			// diagKey = "upgrade_failed" (len 0xe = 14)
			handleInstallError(logger, ctx, "upgrade_failed", outcomes, diagReporter, err, true)
			return err, nil
		}
	} else {
		// No installed version — fresh install
		// 0xae246f-0xae2500: log at INFO level
		// Message length 0x1e = 30: "Installing Claude Code version"
		logger.Info(
			"Installing Claude Code version",
			"targetVersion", version,
		)

		// 0xae252a: call installViaNpm
		err := installViaNpm(logger, ctx, claudePath, version)
		if err != nil {
			// 0xae2850-0xae2895: handleInstallError
			// diagKey = "install_npm" (len 0xb = 11)
			handleInstallError(logger, ctx, "install_npm", outcomes, diagReporter, err, false)
			return err, nil
		}
	}

	// Post-install verification: 0xae2538-0xae2558
	newVersion := getInstalledVersion(claudePath, ctx)
	hadPreviousVersion := installedVersion != ""

	if newVersion != "" {
		// 0xae257f: isSpecificVersion(version)
		if isSpecificVersion(version) && newVersion != version {
			// 0xae25e8-0xae2660: fmt.Errorf with format length 0x38 = 56
			// "version mismatch after installation: expected %s, got %s"
			err := fmt.Errorf("version mismatch after installation: expected %s, got %s", version, newVersion)
			// 0xae2665-0xae26ad: handleInstallError
			// diagKey = "version_mismatch_post" (len 0x14 = 20)
			handleInstallError(logger, ctx, "version_mismatch_post", outcomes, diagReporter, err, hadPreviousVersion)
			return err, nil
		}

		// 0xae26bb-0xae2747: log at INFO level
		// Message length 0x22 = 34: "Claude Code installed successfully"
		logger.Info(
			"Claude Code installed successfully",
			"installedVersion", newVersion,
		)

		// 0xae274c-0xae27d3: fmt.Sprintf with length 0x2f = 47
		// "Claude Code installed successfully (version %s)"
		resultMsg := fmt.Sprintf("Claude Code installed successfully (version %s)", newVersion)
		_ = resultMsg
		return nil, nil
	}

	// 0xae27e2-0xae27f5: no version found after install
	// fmt.Errorf with length 0x23 = 35
	// "srt not found after install, using existing installation"
	// (the actual string from binary matches this)
	err := fmt.Errorf("srt not found after install, using existing installation")
	// 0xae27fa-0xae2842: handleInstallError
	// diagKey = "version_check_failed" (len 0x19 = 25)
	handleInstallError(logger, ctx, "version_check_failed", outcomes, diagReporter, err, hadPreviousVersion)
	return err, nil
}

// handleInstallError logs install errors, reports diagnostics, and updates
// outcomes. If hadPreviousVersion is true, the error is treated as non-fatal
// (the previously installed version can still be used).
//
// Binary address: 0xae2920 - 0xae2ed8
func handleInstallError(
	logger *slog.Logger,
	ctx context.Context,
	diagKey string,
	outcomes interface{},
	diagReporter interface{},
	installErr error,
	hadPreviousVersion bool,
) {
	if hadPreviousVersion {
		// 0xae2975-0xae2978: TESTL R10, R10 — check hadPreviousVersion flag

		// 0xae2980-0xae2a19: create diagnostics map with "exit_code" key
		diagMap := make(map[string]any)
		diagMap["exit_code"] = fmt.Sprintf("%v", installErr)

		// 0xae2a19: diag.LogEnvManagerNoPII
		// diagKey prefix: "claude_code_update_failed_nonfatal" (len 0x22 = 34)
		diag.LogEnvManagerNoPII(logger, "claude_code_update_failed_nonfatal", diagMap)

		// 0xae2a42-0xae2b03: slog.Warn with log message length 0x40 = 64
		// "Claude Code update failed (non-fatal, using existing installation)"
		logger.WarnContext(ctx,
			"Claude Code update failed (non-fatal, using existing installation)",
			"error", installErr,
			"diagKey", diagKey,
		)
	} else {
		// 0xae2c35: hadPreviousVersion is false — fatal error path

		diagMap := make(map[string]any)
		diagMap["exit_code"] = fmt.Sprintf("%v", installErr)

		// diagKey: "claude_code_install_failed" or similar
		diag.LogEnvManagerNoPII(logger, diagKey, diagMap)

		// slog.Error
		logger.ErrorContext(ctx,
			"Claude Code installation failed",
			"error", installErr,
			"diagKey", diagKey,
		)
	}
}
