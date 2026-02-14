// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: cmd/cmd_poll.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd

package cmd

import (
	"github.com/spf13/cobra"
)

// AddPollCommand adds the "poll" subcommand to the root cobra command.
// The poll command performs a single poll request to the API to check for
// available sessions or work items.
//
// Binary: 0xb75e60 - cmd.AddPollCommand
// Source: cmd/cmd_poll.go
//
// Parameters:
//   AX = *cobra.Command (parent/root command)
//
// Flags registered (from pflag calls):
//   --api-url           (0x07=7 chars) StringVar, default "https://api.anthropic.com" (0x19), desc (0x0c=12)
//   --secret-path       (0x0f=15 chars) StringVar, default "", desc (0x36=54 chars)
//   --session-id        (0x0e=14 chars) StringVar, default "", desc (0x3f=63 chars)
//   --work-id           (0x09=9 chars) StringVar, default "", desc (0x76=118 chars)
//   --secret-key-env    (0x10=16 chars) StringVar, default "", desc (0x7b=123 chars)
//   --max-poll-retries  (0x15=21 chars) IntVar, default 0, desc (0x63=99 chars)
//   --log-file          (0x09=9 chars) StringVar, default "mode" (0x04), desc (0x24=36 chars)
func AddPollCommand(rootCmd *cobra.Command) {
	// Allocate flag storage variables.
	// Binary: 0xb75e82-0xb75efa - multiple runtime.newobject calls (7 allocations)
	var apiURL string       // offset 0x80
	var secretPath string   // offset 0x68
	var sessionID string    // offset 0x78
	var workID string       // offset 0x50
	var secretKeyEnv string // offset 0x58
	var maxPollRetries int  // offset 0x60
	var logFile string      // offset 0x70

	// Create the cobra.Command.
	// Binary: 0xb75eff-0xb75f5b
	// Use: "poll" (0x04=4 chars)
	// Short: 0x23=35 chars
	// Long: 0x2eb=747 chars
	// Example: 0x299=665 chars
	pollCmd := &cobra.Command{
		Use:   "poll",
		Short: "Poll the API for available sessions",
		Long:  `Poll the API for available sessions or work items. This command makes a single poll request to the environment service API and outputs the result. It's useful for testing API connectivity and seeing what work is available. For continuous polling, use the 'orchestrator' command instead.`,
		Example: `  # Basic polling
  environment-runner poll --api-url=https://api.example.com --session-id=abc --work-id=xyz

  # With service key file
  environment-runner poll --api-url=https://api.example.com --session-id=abc --secret-path=/path/to/key`,
		RunE: func(cmd *cobra.Command, args []string) error {
			// Binary: 0xb76200 - AddPollCommand.func1
			_ = apiURL
			_ = secretPath
			_ = sessionID
			_ = workID
			_ = secretKeyEnv
			_ = maxPollRetries
			_ = logFile
			return nil
		},
	}

	// Register all flags.
	// Binary: 0xb7602e-0xb7619a
	pollCmd.Flags().StringVar(&apiURL, "api-url", "https://api.anthropic.com", "API base URL")
	pollCmd.Flags().StringVar(&secretPath, "secret-path", "", "Path to environment service key file for API authentication. Falls back to ENVIRONMENT_SERVICE_KEY env var if not set.")
	pollCmd.Flags().StringVar(&sessionID, "session-id", "", "Session identifier for polling. Required for session-based orchestration.")
	pollCmd.Flags().StringVar(&workID, "work-id", "", "Work identifier for filtering poll results. When specified, only returns work matching this ID. Path to environment service key file for API healthcheck.")
	pollCmd.Flags().StringVar(&secretKeyEnv, "secret-key-env", "", "Environment variable name containing the service key. Alternative to --secret-path for environments where file-based secrets are not available.")
	pollCmd.Flags().IntVar(&maxPollRetries, "max-poll-retries", 0, "Maximum number of consecutive poll failures before giving up. Each retry uses exponential backoff.")
	pollCmd.Flags().StringVar(&logFile, "log-file", "mode", "The log file path for diagnostic output")

	// Add to root command.
	rootCmd.AddCommand(pollCmd)
}
