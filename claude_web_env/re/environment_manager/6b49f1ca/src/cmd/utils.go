// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: cmd/utils.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd

package cmd

import (
	"fmt"
	"log/slog"
	"net/url"

	"github.com/spf13/cobra"
)

// parseLogLevel converts a log level string to the corresponding slog.Level.
// Valid levels: "debug" (-4), "info" (0), "warn" (4), "error" (8).
//
// Binary: 0xb7c6c0 - cmd.parseLogLevel
// Source: cmd/utils.go
//
// Parameters:
//
//	AX = string data pointer
//	BX = string length
//
// Returns:
//
//	AX = slog.Level value
//	BX = error interface type (0 if nil)
//	CX = error interface data (0 if nil)
//
// String matching (direct 4-byte CMPL comparisons):
//
//	"info" = 0x6f666e69 -> Level 0 (slog.LevelInfo)
//	"warn" = 0x6e726177 -> Level 4 (slog.LevelWarn)
//	"debug" = 0x75626564 + 'g' -> Level -4 (slog.LevelDebug)
//	"error" = 0x6f727265 + 'r' -> Level 8 (slog.LevelError)
func parseLogLevel(level string) (slog.Level, error) {
	switch level {
	case "info":
		return slog.LevelInfo, nil
	case "warn":
		return slog.LevelWarn, nil
	case "debug":
		return slog.LevelDebug, nil
	case "error":
		return slog.LevelError, nil
	default:
		return 0, fmt.Errorf("invalid log level: %s (valid: debug, info, warn, error)", level)
	}
}

// validateAPIBaseURL validates that a given API URL is well-formed with a scheme
// and host. Returns an error if the URL is empty, fails to parse, or has no host.
//
// Binary: 0xb7c140 - cmd.validateAPIBaseURL
// Source: cmd/cmd_task_run.go (per binary debug info, but logically a utility)
//
// Parameters:
//
//	AX = string data pointer (URL)
//	BX = string length
//
// Returns:
//
//	AX = error interface type (0 if nil)
//	BX = error interface data (0 if nil)
//
// Flow:
//  1. If BX == 0 (empty string): return fmt.Errorf("empty API base URL")
//  2. Call net/url.Parse(AX, BX)
//  3. If parse error: return fmt.Errorf("invalid API base URL: %w", err)
//  4. If parsed.Host == "": return fmt.Errorf("API base URL has no host: %s", url)
//  5. Return nil
func validateAPIBaseURL(apiURL string) error {
	if apiURL == "" {
		return fmt.Errorf("empty API base URL")
	}

	parsed, err := url.Parse(apiURL)
	if err != nil {
		return fmt.Errorf("invalid API base URL: %w", err)
	}

	if parsed.Host == "" {
		return fmt.Errorf("API base URL has no host: %s", apiURL)
	}

	return nil
}

// markRequiredFlags marks the given flag names as required on the cobra command.
// Panics if a flag cannot be marked as required, indicating a programming error.
//
// Binary: 0xb7c7c0 - cmd.markRequiredFlags
// Source: cmd/utils.go
//
// Parameters:
//
//	AX = *cobra.Command
//	BX = []string data pointer (flag names)
//	CX = []string length
//	DI = []string capacity
//
// Flow:
//
//	Iterates over the flag names slice. For each flag name:
//	  1. Calls cobra.(*Command).MarkFlagRequired(AX, flagName)
//	  2. If error is non-nil: panics with
//	     fmt.Errorf("failed to mark %s as required: %w", flagName, err)
//	  3. Otherwise continues to next flag
func markRequiredFlags(cmd *cobra.Command, flags ...string) {
	for _, flag := range flags {
		err := cmd.MarkFlagRequired(flag)
		if err != nil {
			panic(fmt.Errorf("failed to mark %s as required: %w", flag, err))
		}
	}
}
