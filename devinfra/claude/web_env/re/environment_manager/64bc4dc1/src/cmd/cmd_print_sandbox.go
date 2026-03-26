// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: cmd/cmd_print_sandbox.go (inferred from function placement)
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd
// Updated in a6f96673: now called from inline cmd in main.go rather than via AddCommand.
// Carried forward unchanged to 64bc4dc1.

package cmd

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/sandbox"
)

// runPrintSandboxSettings outputs the current sandbox configuration as JSON
// to stdout. This is used for debugging and verification of sandbox settings.
//
// Binary: 0xb76bc0 - cmd.runPrintSandboxSettings
// Source: cmd/cmd_print_sandbox.go
//
// The command name observed in strings: "print-sandbox-settings"
// Usage example: "environment-runner print-sandbox-settings > my-sandbox-settings.json"
// RunPrintSandboxSettings is the exported wrapper called from main.go's inline command.
func RunPrintSandboxSettings() error {
	return runPrintSandboxSettings()
}

func runPrintSandboxSettings() error {
	// Retrieves the current sandbox configuration
	// Marshals it to JSON with indentation
	// Writes to stdout

	settings := getSandboxSettings()

	data, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal sandbox settings: %w", err)
	}

	_, err = os.Stdout.Write(data)
	if err != nil {
		return fmt.Errorf("failed to write sandbox settings: %w", err)
	}

	fmt.Println() // trailing newline
	return nil
}

// getSandboxSettings returns the current sandbox configuration as a
// structured object for JSON serialization.
//
// The binary constructs this struct inline in runPrintSandboxSettings
// rather than calling a separate function. The settings are hardcoded
// constants representing the default sandbox configuration.
//
// Assembly analysis of runPrintSandboxSettings (0xb76bc0-0xb76e61):
//
//	Struct 1 (4 fields): AllowWrite paths
//	  /tmp (4), /tmp/claude (11), "" (empty), /workspace (10)
//	Struct 2 (3 fields): AllowedDomains
//	  api.anthropic.com (17), api-staging.anthropic.com (25), *.anthropic.com (15)
//	Struct 3 (6 fields): DenyRead paths
//	  ~/.ssh (6), ~/.aws (6), ~/.config/gcloud (16), /etc/shadow (11), /etc/passwd- (12), /secrets (8)
//	Final assembly: SandboxConfig with:
//	  AllowedDomains = Struct 2 (3 entries, cap 3)
//	  DenyRead = Struct 3 (6 entries, cap 6)
//	  AllowWrite = Struct 1 (4 entries, cap 4)
//	  EnableWeakerNestedSandbox = true (0xb76da5: MOVB $0x1, 0x78(AX))
func getSandboxSettings() interface{} {
	return &sandbox.SandboxConfig{
		AllowedDomains: []string{
			"api.anthropic.com",
			"api-staging.anthropic.com",
			"*.anthropic.com",
		},
		DenyRead: []string{
			"~/.ssh",
			"~/.aws",
			"~/.config/gcloud",
			"/etc/shadow",
			"/etc/passwd-",
			"/secrets",
		},
		AllowWrite: []string{
			"/tmp",
			"/tmp/claude",
			"",
			"/workspace",
		},
		EnableWeakerNestedSandbox: true,
	}
}
