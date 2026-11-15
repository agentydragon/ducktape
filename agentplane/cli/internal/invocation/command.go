package invocation

import (
	"fmt"

	"github.com/spf13/cobra"
)

func RequireSubcommand(cmd *cobra.Command, _ []string) error {
	fmt.Fprint(cmd.ErrOrStderr(), cmd.UsageString())
	return UsageFailure("%s needs a command", cmd.CommandPath())
}

// group is a command that only holds subcommands.
func Group(use, short, long string, children ...*cobra.Command) *cobra.Command {
	command := &cobra.Command{Use: use, Short: short, Long: long, Args: cobra.NoArgs, RunE: RequireSubcommand}
	command.AddCommand(children...)
	return command
}
