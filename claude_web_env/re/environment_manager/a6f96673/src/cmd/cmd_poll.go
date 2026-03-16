// Reconstructed from binary: environment-manager (Build ID a6f96673)
// Source: cmd/cmd_poll.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd

package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/logger"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/orchestrator"
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
//
//	AX = *cobra.Command (parent/root command)
//
// Flags registered (from pflag calls):
//
//	--api-url           (0x07=7 chars) StringVar, default "https://api.anthropic.com" (0x19), desc (0x0c=12)
//	--secret-path       (0x0f=15 chars) StringVar, default "", desc (0x36=54 chars)
//	--session-id        (0x0e=14 chars) StringVar, default "", desc (0x3f=63 chars)
//	--work-id           (0x09=9 chars) StringVar, default "", desc (0x76=118 chars)
//	--secret-key-env    (0x10=16 chars) StringVar, default "", desc (0x7b=123 chars)
//	--max-poll-retries  (0x15=21 chars) IntVar, default 0, desc (0x63=99 chars)
//	--log-file          (0x09=9 chars) StringVar, default "mode" (0x04), desc (0x24=36 chars)
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

			// Step 1: Parse log level and create logger.
			// Binary: 0xb76278 parseLogLevel, 0xb7628a CreateLoggerWithFileOutput
			level, err := parseLogLevel(logFile)
			if err != nil {
				return err
			}
			log := logger.CreateLoggerWithFileOutput(level)

			// Step 2: Read service key from file or env var.
			// Binary: 0xb762a3-0xb76398 - ReadFile + TrimSpace + Getenv fallback
			// Same pattern as loadServiceKey: if secretPath non-empty, ReadFile it,
			// on error return fmt.Errorf("failed to read service key file %s: %w"),
			// TrimSpace result, if empty fall back to env var (secretKeyEnv or ENVIRONMENT_SERVICE_KEY).
			var serviceKey string
			if secretPath != "" {
				data, err := os.ReadFile(secretPath)
				if err != nil {
					return fmt.Errorf("failed to read service key file %s: %w", secretPath, err)
				}
				serviceKey = strings.TrimSpace(string(data))
			}
			if serviceKey == "" {
				// Use custom env var name if specified, otherwise default to ENVIRONMENT_SERVICE_KEY
				envVarName := "ENVIRONMENT_SERVICE_KEY"
				if secretKeyEnv != "" {
					envVarName = secretKeyEnv
				}
				serviceKey = os.Getenv(envVarName)
			}

			// Step 3: Check service key is available.
			// Binary: 0xb763a9 JE to error at 0xb76b4e if serviceKey empty
			if serviceKey == "" {
				return fmt.Errorf("session_id and org_id are required when identity could not be retrieved")
			}

			// Step 4: Create WhoamiClient and get identity.
			// Binary: 0xb763f8 NewWhoamiClient, 0xb76407 GetIdentity
			whoamiClient := orchestrator.NewWhoamiClient(apiURL, serviceKey, sessionID, log)
			identity, err := whoamiClient.GetIdentity(cmd.Context())
			if err != nil {
				// Step 4a: Check if sessionID and workID were provided.
				// Binary: 0xb76415-0xb76555 - checks closed-over sessionID and workID ptrs
				// If both are non-empty: skip the error and proceed
				if sessionID != "" && workID != "" {
					// Identity not needed, continue with provided session/work IDs
				} else {
					// Error: "session_id and org_id are required when identity could not be retrieved"
					// Binary: 0xb76577 - 0x73=115 chars error message
					return fmt.Errorf("session_id and org_id are required when identity could not be retrieved")
				}
			} else {
				// Step 4b: Update sessionID and workID from identity.
				// Binary: 0xb765a1-0xb7669e - compares with memequal and updates if different
				// This updates the closed-over variables sessionID and workID with identity values
				sessionID = identity.SessionID
				workID = identity.OrgID
			}

			// Step 5: Log poll info.
			// Binary: 0xb766c5-0xb76780 - slog.Info with 4 attrs:
			//   "session_id", "org_id" at slog level 4 (Info)
			//   Message: "Starting poll with identity and session details" (0x29=41 chars)
			log.Info("Starting poll with identity and session details",
				"session_id", sessionID,
				"org_id", workID,
			)

			// Step 6: Create poller and execute single poll.
			// Binary: 0xb767d9 NewPollerWithWorkerID, 0xb767e8 Poll
			poller := orchestrator.NewPollerWithWorkerID(apiURL, sessionID, serviceKey, "", "", log)
			session, err := poller.Poll(cmd.Context())
			if err != nil {
				// Binary: 0xb767f0-0xb7683a
				// fmt.Errorf("poll command failed: %w") (0x17=23 chars)
				return fmt.Errorf("poll command failed: %w", err)
			}

			// Step 7: Output result.
			// Binary: 0xb76840-0xb76a28
			if session == nil {
				// No session available - print empty message.
				fmt.Fprintln(os.Stdout, "")
			} else {
				// Try to pretty-print as JSON.
				// Binary: 0xb76862-0xb768f6 - json.Unmarshal into new object
				// Binary: 0xb76908 json.MarshalIndent with "  " prefix
				indented, err := json.MarshalIndent(session, "", "  ")
				if err != nil {
					// Fall back to raw bytes.
					fmt.Fprintln(os.Stdout, session)
				} else {
					fmt.Fprintln(os.Stdout, string(indented))
				}
			}

			// Step 8: Return success.
			// Binary: 0xb76a28 XORL AX, AX; XORL BX, BX
			// Note: maxPollRetries is currently unused - Poller struct has no retry field.
			// Retries may be handled at a higher level (orchestrator) rather than in Poller.Poll().
			_ = maxPollRetries
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
