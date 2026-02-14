// Reconstructed from symbol: main.main, main.Version
// Source: /home/runner/work/anthropic/anthropic/api-go/environment-manager/main.go
package main

import (
	"fmt"
	"os"

	"github.com/anthropics/anthropic/api-go/environment-manager/cmd"
	"github.com/spf13/cobra"
)

// Version is set via -ldflags "-X main.Version=staging-7c3cd5476"
var Version = "dev"

func main() {
	rootCmd := &cobra.Command{
		Use:   "environment-runner",
		Short: "Environment manager for Claude Code web containers",
	}

	rootCmd.Version = Version

	cmd.AddSetupCommand(rootCmd)
	cmd.AddOrchestratorCommand(rootCmd)
	cmd.AddTaskRunCommand(rootCmd)
	cmd.AddPollCommand(rootCmd)

	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
