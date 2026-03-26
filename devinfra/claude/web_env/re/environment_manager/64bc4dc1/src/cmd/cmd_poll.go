// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
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
// Flags registered (from live binary --help):
//
//	--api-url               StringVar, default "https://api.anthropic.com"
//	--environment-id        StringVar
//	--log-level             StringVar, default "info"
//	--organization-id       StringVar
//	--reclaim-older-than-ms IntVar, default 0
//	--service-key-file      StringVar
//	--worker-id             StringVar
func AddPollCommand(rootCmd *cobra.Command) {
	// Allocate flag storage variables.
	// Binary: 0xb75e82-0xb75efa - multiple runtime.newobject calls
	var apiURL string
	var environmentID string
	var logLevel string
	var organizationID string
	var reclaimOlderThanMs int
	var serviceKeyFile string
	var workerID string

	// Create the cobra.Command.
	// Binary: 0xb75eff-0xb75f5b
	// Use: "poll" (0x04=4 chars)
	// Short: 0x23=35 chars
	// Long: 0x2eb=747 chars
	// Example: 0x299=665 chars
	pollCmd := &cobra.Command{
		Use:   "poll",
		Short: "Make a single poll request for work",
		Long: `The poll command makes a single non-blocking request to the API for work.

This is a thin wrapper around the poll endpoint that returns work JSON on stdout
if work is available, or "null" if the queue is empty.

The worker_id parameter enables per-worker IDs for better observability
and faster reclaim of unacknowledged work. If not provided, it defaults to the
system hostname. The worker ID is sent via the Anthropic-Worker-ID HTTP header.

Required environment variable:
  ENVIRONMENT_SERVICE_KEY: Service key for the environment

The environment ID and organization ID are discovered automatically via the whoami
endpoint. You can optionally provide --environment-id and --organization-id flags
to validate them against the token's identity.`,
		Example: `  # Basic usage (identity discovered via whoami, worker-id defaults to hostname)
  export ENVIRONMENT_SERVICE_KEY="your-environment-service-key"
  environment-runner poll

  # With explicit worker ID
  export ENVIRONMENT_SERVICE_KEY="your-environment-service-key"
  environment-runner poll --worker-id "worker-$(hostname)-$$"

  # With explicit IDs for validation
  export ENVIRONMENT_SERVICE_KEY="your-environment-service-key"
  environment-runner poll \
    --worker-id "worker-123" \
    --environment-id "env_01ABC123" \
    --organization-id "org_01XYZ789"

  # Read service key from file
  environment-runner poll \
    --service-key-file /path/to/service-key`,
		RunE: func(cmd *cobra.Command, args []string) error {
			// Binary: 0xb76200 - AddPollCommand.func1

			// Step 1: Parse log level and create logger.
			// Binary: 0xb76278 parseLogLevel, 0xb7628a CreateLoggerWithFileOutput
			level, err := parseLogLevel(logLevel)
			if err != nil {
				return err
			}
			log := logger.CreateLoggerWithFileOutput(level)

			// Step 2: Read service key from file or env var.
			// Binary: 0xb762a3-0xb76398 - ReadFile + TrimSpace + Getenv fallback
			// Same pattern as loadServiceKey: if serviceKeyFile non-empty, ReadFile it,
			// on error return fmt.Errorf("failed to read service key file %s: %w"),
			// TrimSpace result, if empty fall back to ENVIRONMENT_SERVICE_KEY env var.
			var serviceKey string
			if serviceKeyFile != "" {
				data, err := os.ReadFile(serviceKeyFile)
				if err != nil {
					return fmt.Errorf("failed to read service key file %s: %w", serviceKeyFile, err)
				}
				serviceKey = strings.TrimSpace(string(data))
			}
			if serviceKey == "" {
				serviceKey = os.Getenv("ENVIRONMENT_SERVICE_KEY")
			}

			// Step 3: Check service key is available.
			// Binary: 0xb763a9 JE to error at 0xb76b4e if serviceKey empty
			if serviceKey == "" {
				return fmt.Errorf("service key is required: use --service-key-file or set ENVIRONMENT_SERVICE_KEY environment variable\n" +
					"  export ENVIRONMENT_SERVICE_KEY=\"your-environment-service-key\"\n" +
					"Or use --service-key-file to read from a file:\n" +
					"  --service-key-file /path/to/service-key\n" +
					"You can create and manage environment service keys at claude.ai/settings\n" +
					"under your environment's settings")
			}

			// Step 4: Create WhoamiClient and get identity.
			// Binary: 0xb763f8 NewWhoamiClient, 0xb76407 GetIdentity
			whoamiClient := orchestrator.NewWhoamiClient(apiURL, serviceKey, environmentID, log)
			identity, err := whoamiClient.GetIdentity(cmd.Context())
			if err != nil {
				// Step 4a: Check if environmentID and organizationID were provided.
				// Binary: 0xb76415-0xb76555 - checks closed-over environmentID and organizationID ptrs
				// If both are non-empty: skip the error and proceed
				if environmentID != "" && organizationID != "" {
					// Identity not needed, continue with provided IDs
				} else {
					// Binary: 0xb76577 — wraps whoami error with guidance to use fallback flags
					return fmt.Errorf("failed to get environment identity from whoami (provide --organization-id and --environment-id to use fallback): %w", err)
				}
			} else {
				// Step 4b: Update environmentID and organizationID from identity.
				// Binary: 0xb765a1-0xb7669e - compares with memequal and updates if different
				// This updates the closed-over variables with identity values
				environmentID = identity.SessionID
				organizationID = identity.OrgID
			}

			// Step 5: Log poll info.
			// Binary: 0xb766c5-0xb76780 - slog.Info with 4 attrs:
			//   "environment_id", "organization_id" at slog level 4 (Info)
			//   Message: "Starting poll with identity and session details" (0x29=41 chars)
			log.Info("Starting poll with identity and session details",
				"environment_id", environmentID,
				"organization_id", organizationID,
			)

			// Step 6: Create poller and execute single poll.
			// Binary: 0xb767d9 NewPollerWithWorkerID, 0xb767e8 Poll
			poller := orchestrator.NewPollerWithWorkerID(apiURL, environmentID, serviceKey, workerID, "", log)
			session, err := poller.Poll(cmd.Context())
			if err != nil {
				// Binary: 0xb767f0-0xb7683a
				// fmt.Errorf("poll command failed: %w") (0x17=23 chars)
				return fmt.Errorf("poll command failed: %w", err)
			}

			// Step 7: Output result.
			// Binary: 0xb76840-0xb76a28
			if session == nil {
				// No session available - print "null" (matches Long description).
				fmt.Fprintln(os.Stdout, "null")
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
			return nil
		},
	}

	// Register all flags.
	// Binary: 0xb7602e-0xb7619a
	pollCmd.Flags().StringVar(&apiURL, "api-url", "https://api.anthropic.com", "API base URL")
	pollCmd.Flags().StringVar(&environmentID, "environment-id", "", "Environment ID (find at claude.ai/settings, e.g., env_01ABC123)")
	pollCmd.Flags().StringVar(&logLevel, "log-level", "info", "Log level (debug, info, warn, error)")
	pollCmd.Flags().StringVar(&organizationID, "organization-id", "", "Organization ID (find at claude.ai/settings > Account)")
	pollCmd.Flags().IntVar(&reclaimOlderThanMs, "reclaim-older-than-ms", 0, "Reclaim unacknowledged work items older than this many milliseconds (0 = use API default of 5000ms)")
	pollCmd.Flags().StringVar(&serviceKeyFile, "service-key-file", "", "Path to file containing the environment service key. If not set, falls back to ENVIRONMENT_SERVICE_KEY environment variable")
	pollCmd.Flags().StringVar(&workerID, "worker-id", "", "Unique worker identifier for this worker. Sent via Anthropic-Worker-ID header. If not set, defaults to system hostname")

	// Add to root command.
	rootCmd.AddCommand(pollCmd)
}
