// Reconstructed from binary: environment-manager (Build ID 495ea204)
// Source: internal/claude/init.go
// Original path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/claude/init.go

package claude

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os/exec"
	"syscall"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y/diag"
)

// RunInit executes "claude --init-only" in the specified working directory
// with the given environment variables. It measures execution time and reports
// outcomes via structured logging and diagnostic events.
//
// The --init-only flag (hidden, 11 chars matching 0xb in binary) tells Claude
// Code to run Setup + SessionStart hooks synchronously, then exit:
//  1. applyConfigEnvironmentVariables()
//  2. processSetupHooks('init', { forceSyncExecution: true })
//  3. processSessionStartHooks('startup', { forceSyncExecution: true })
//  4. gracefulShutdownSync(0)
//
// This fires our SessionStart hook BEFORE the main Claude Code session starts.
// The main session (launched by ClaudeCodeExecutor.Execute with --resume) fires
// its own SessionStart hooks during startup, so hooks fire TWICE per new session.
// Our hook daemon is idempotent, so the second invocation is a fast no-op.
//
// Session mode gating: RunInit is called for ALL session modes (new, setup-only,
// resume, resume-cached). The session_mode.go documentation confirms that even
// resume modes run RunInit.
//
// Error handling: RunInit errors are NON-FATAL. The caller in
// anthropicEnvironmentType.Initialize logs the error at Warn level and
// continues with skills/hooks bootstrapping. RunInit itself returns an error,
// but Initialize does not propagate it.
//
// Call chain (495ea204 binary):
//
//	Manager.initializeEnvironmentAsync (manager/manager.go, 0xb6f2a0)
//	  → envType.Initialize(ctx)  (vtable dispatch at 0xb6f44c)
//	    → anthropicEnvironmentType.Initialize (envtype/anthropic/anthropic.go)
//	      → RunInit (claude/init.go)
//
// Parameters (from register mapping at 0xae0b00):
//   - AX: logger (*slog.Logger)
//   - BX: logger (continued - Go slog.Logger is passed as interface)
//   - CX: ctx (context.Context, interface value)
//   - DI: ctx (context.Context, continued)
//   - SI: workingDir (string data ptr)
//   - R8: envVars (map[string]string)
//
// Binary address: 0xae0b00 - 0xae14a3 (a6f96673; address likely shifted in 495ea204)
func RunInit(
	logger *slog.Logger,
	ctx context.Context,
	workingDir string,
	envVars map[string]string,
) error {
	// 0xae0b4d: calls GetClaudePath
	claudePath, _ := GetClaudePath(logger, ctx, nil)

	// 0xae0c40: slog.Info "Running init script with claude and working directory"
	// Log message length 0x32 = 50 chars
	logger.InfoContext(ctx,
		"Running init script with claude and working directory",
		"claudePath", claudePath,
		"workingDir", workingDir,
	)

	// 0xae0c45: time.Now()
	startTime := time.Now()

	// 0xae0c62-0xae0cae: exec.CommandContext(ctx, claudePath, "--init-only")
	// string at 0x228(SP) length 0xb = 11 chars = "--init-only"
	// The garble obfuscator encrypts this string at rest; it's decrypted at runtime.
	// "--init-only" is a hidden Claude Code CLI flag that runs Setup + SessionStart
	// hooks synchronously, then exits (gracefulShutdownSync(0)).
	// Previous RE incorrectly identified this as "init" (4 chars doesn't match 0xb=11).
	cmd := exec.CommandContext(ctx, claudePath, "--init-only")

	// 0xae0cbf: cmd.Dir = workingDir (offset 0x48 in exec.Cmd)
	cmd.Dir = workingDir

	// 0xae0cf6: syscall.Environ()
	// 0xae0d03-0xae0d24: cmd.Env = syscall.Environ()
	cmd.Env = syscall.Environ()

	// 0xae0d28-0xae0d33: check if envVars != nil
	if envVars != nil {
		// 0xae0d35-0xae12e5: iterate map, format "key=value", append to cmd.Env
		for k, v := range envVars {
			cmd.Env = append(cmd.Env, fmt.Sprintf("%s=%s", k, v))
		}
	}

	// 0xae0d71-0xae0d74: cmd.CombinedOutput()
	output, err := cmd.CombinedOutput()

	// 0xae0dab: time.Since(startTime)
	elapsed := time.Since(startTime)

	if err != nil {
		// 0xae0dbe-0xae0e36: extract exit code via errors.As(*exec.ExitError)
		// Uses IMULQ magic number 0x431bde82d7b634db for nanoseconds-to-milliseconds division
		elapsedMs := elapsed.Milliseconds()

		exitCode := -1
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			if exitErr.ProcessState != nil {
				ws := exitErr.ProcessState.ExitCode()
				if ws&0x7f != 0 {
					exitCode = -1
				} else {
					exitCode = (ws >> 8) & 0xff
				}
			}
		}

		// 0xae0fc9: slog.Warn "claude init failed" (level 8 = WARN)
		// Log message length 0x19 = 25 chars
		logger.WarnContext(ctx,
			"claude init failed",
			"error", err,
			"exitCode", exitCode,
			"elapsedMs", elapsedMs,
			"output", string(output),
		)

		// 0xae10e0: diag.LogEnvManagerNoPII "claude_init_failed"
		// string length 0x12 = 18 chars
		diag.LogEnvManagerNoPII(logger, "claude_init_failed", map[string]any{
			"exit_code":  exitCode,
			"elapsed_ms": elapsedMs,
		})

		// 0xae1163: fmt.Errorf with format length 0x2f = 47
		return fmt.Errorf(
			"claude init failed with exit code %d: %w",
			exitCode, err,
		)
	}

	// Success path: 0xae1171
	elapsedMs := elapsed.Milliseconds()

	// 0xae1218: slog.Info "claude init completed successfully"
	// Log message length 0x29 = 41 chars
	logger.InfoContext(ctx,
		"claude init completed successfully",
		"elapsedMs", elapsedMs,
	)

	// 0xae12c0: diag.LogEnvManagerNoPII "claude_init_success"
	// string length 0x13 = 19 chars
	diag.LogEnvManagerNoPII(logger, "claude_init_success", map[string]any{
		"elapsed_ms": elapsedMs,
	})

	return nil
}
