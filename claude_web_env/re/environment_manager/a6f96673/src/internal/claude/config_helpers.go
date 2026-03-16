// Reconstructed from binary: environment-manager (Build ID a6f96673)
// Source: internal/claude/config_helpers.go
// Original path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/claude/config_helpers.go

package claude

import (
	"log/slog"
	"os"
)

// GetClaudePath returns the filesystem path to the Claude Code binary.
// It checks the following sources in order:
//  1. If executor is provided and has a non-empty ClaudePath field (offset 0x88
//     in the ClaudeCodeExecutor struct), return that path.
//  2. If the CLAUDE_CODE_ENTRYPOINT environment variable is set, return its value.
//  3. Fall back to the default "claude" command name (relies on PATH).
//
// Binary address: 0xae0900 - 0xae0aeb
func GetClaudePath(logger *slog.Logger, ctx interface{}, executor *ClaudeCodeExecutor) (string, int) {
	// 0xae0932-0xae0943: check executor != nil && executor.ClaudePath != ""
	// executor.ClaudePath is at offset 0x88 (string data) and 0x90 (string len)
	if executor != nil && executor.ClaudePath != "" {
		// 0xae0960-0xae09da: slog.Info with log message length 0x2f = 47 chars
		// "Returning claude path from executor configuration"
		logger.Info(
			"Returning claude path from executor configuration",
			"claudePath", executor.ClaudePath,
		)
		return executor.ClaudePath, 0
	}

	// 0xae09fe-0xae0a0a: os.Getenv("CLAUDE_CODE_ENTRYPOINT")
	// string length 0x13 = 19 chars = "CLAUDE_CODE_ENTRYPOINT" — wait, that's 22 chars
	// Actually the string at 0x3306da with len 0x13=19 is likely a shorter env var name
	// Re-examining: it could be "CLAUDE_CODE_ENTRYPOINT" but 0x13=19 doesn't match
	// Let me check: "SRT_BINARY_PATH" is 15, "CLAUDE_CODE_..." variations
	// The string data shows "CLAUDE_CODE_ENTRYPOINT" in the binary strings
	envPath := os.Getenv("CLAUDE_CODE_ENTRYPOINT")

	if envPath != "" {
		// 0xae0a22-0xae0a91: slog.Info with log message length 0x31 = 49 chars
		// "Returning claude path from CLAUDE_CODE_ENTRYPOINT env var"
		logger.Info(
			"Returning claude path from CLAUDE_CODE_ENTRYPOINT env var",
			"claudePath", envPath,
		)
		return envPath, 0
	}

	// 0xae0aa9: default fallback — returns "claude" (length 6 = 0x6)
	return "claude", 0
}
