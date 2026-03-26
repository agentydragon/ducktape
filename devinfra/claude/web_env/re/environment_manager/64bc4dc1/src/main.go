// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: /home/runner/work/anthropic/anthropic/api-go/environment-manager/main.go
// Reconstructed for binary 64bc4dc1.
//
// NOTE: Binary 64bc4dc1 is garble-obfuscated (no DWARF, no symbol table).
// Binary addresses in this file are from the a6f96673 predecessor and are
// meaningless for the garble-obfuscated 64bc4dc1 binary. Structure and
// behavior are inferred from runtime output and string analysis only.
//
// Key changes in 64bc4dc1 vs a6f96673:
//   - Version string changed: staging-68f0dff496 -> release-9f4ec76fbc-ext
//   - Channel changed: staging -> release (production)
//   - Binary size: 27 MB -> 49 MB (garble inlines and pads code)
//   - Binary obfuscated with garble: no DWARF debug info, no symbol table
//   - go version -m returns "unknown" (garble strips module info)
//   - go tool nm returns no output (symbol table garbled)
//   - Obfuscated symbol names visible in strings (e.g., qbbw3lR, pVHE5Urql8v)
//   - CLI behavior verified unchanged via --help and print-sandbox-settings
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/anthropics/anthropic/api-go/environment-manager/cmd"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
	"github.com/spf13/cobra"
)

// Version is set via -ldflags "-X main.Version=release-9f4ec76fbc-ext"
var Version = "dev"

func main() {
	// Binary: 0xbb95d2 (line 21) - Copy version to internal/util.Version
	// This allows other packages to access the version string.
	util.Version = Version

	// Binary: 0xbb960a-0xbb9676 (lines 24-28) - Check if invoked as "code-sign"
	// Checks if the binary basename is exactly "code-sign" (9 chars)
	// or if the basename ends with "code-sign" (suffix check with 10 chars for "/code-sign")
	baseName := filepath.Base(os.Args[0])
	isCodeSign := baseName == "code-sign" || strings.HasSuffix(baseName, "code-sign")

	if isCodeSign {
		// Binary: 0xbb967e-0xbb98f3 (line 30) - Direct code-sign mode
		// Creates a minimal cobra command with os.Args[1:] and runs code-sign
		codeSignCmd := &cobra.Command{
			Use:   "code-sign",
			Short: "Sign code artifacts using MCP code-sign server",
			Long:  `Sign code artifacts using the MCP code-sign server. This command is typically invoked as a symlink named "code-sign" and handles GPG/SSH signing operations for git commits and tags.`,
			RunE: func(cobraCmd *cobra.Command, args []string) error {
				return cmd.RunCodeSignFromMain(cobraCmd.Context(), args)
			},
			// Hidden: true (0x2b1)
			// DisableFlagParsing: true (0x2b4)
		}
		codeSignCmd.Hidden = true
		codeSignCmd.DisableFlagParsing = true

		// Execute directly
		if err := codeSignCmd.Execute(); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}

	// Binary: 0xbb97ec (line 33) - Create root command
	rootCmd := &cobra.Command{
		Use:   "environment-runner",
		Short: "Environment manager for Claude Code web containers",
		Long:  "Environment manager that orchestrates Claude Code web container sessions, handling setup, task execution, and lifecycle management.",
	}

	rootCmd.Version = Version

	// Binary: 0xbb9862-0xbb9876 (lines 40-42) - Add subcommands via functions
	cmd.AddOrchestratorCommand(rootCmd)
	cmd.AddTaskRunCommand(rootCmd)
	cmd.AddPollCommand(rootCmd)

	// Binary: 0xbb987c-0xbb98f3 (line 43, source: cmd_code_sign.go:112-125)
	// code-sign subcommand added inline (hidden, with DisableFlagParsing)
	codeSignCmd := &cobra.Command{
		Use:   "code-sign",
		Short: "Set environment variables for code-sign",
		Long:  `Sign code artifacts using the MCP code-sign server. Handles GPG and SSH signing operations for git commits and tags via the code-sign MCP server.`,
		RunE: func(c *cobra.Command, args []string) error {
			return cmd.RunCodeSignFromMain(c.Context(), args)
		},
	}
	codeSignCmd.Hidden = true
	codeSignCmd.DisableFlagParsing = true
	rootCmd.AddCommand(codeSignCmd)

	// Binary: 0xbb98f9-0xbb9975 (line 44, source: cmd_print_sandbox_settings.go:15-35)
	// print-sandbox-settings subcommand added inline
	printSandboxCmd := &cobra.Command{
		Use:   "print-sandbox-settings",
		Short: "Print default sandbox configuration",
		Long:  `Print the default sandbox configuration as JSON to stdout. This command outputs the sandbox settings that would be used by the task-run and orchestrator commands when no custom sandbox settings are provided.`,
		Example: `  environment-runner print-sandbox-settings
  environment-runner print-sandbox-settings | jq .`,
		RunE: func(c *cobra.Command, args []string) error {
			return cmd.RunPrintSandboxSettings()
		},
	}
	rootCmd.AddCommand(printSandboxCmd)

	// Binary: 0xbb9980 (line 45) - Add setup command
	cmd.AddSetupCommand(rootCmd)

	// Binary: 0xbb998a - Execute root command
	// Note: Cobra automatically adds "completion" subcommand for shell completions
	if err := rootCmd.Execute(); err != nil {
		// Binary: 0xbb9994-0xbb99d2 (line 48) - Print error to stderr
		fmt.Fprintln(os.Stderr, err)
		// Binary: 0xbb99d7-0xbb99e0 (line 49) - Exit with code 1
		os.Exit(1)
	}
}
