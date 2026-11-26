package invocation

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"
	"google.golang.org/grpc"
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

// GRPCCommand connects one live command to its service and gives its handler the invocation deadline.
func GRPCCommand[C any](
	a *Invocation, group string, newClient func(grpc.ClientConnInterface) C, run func(context.Context, C, []string) error,
) func(*cobra.Command, []string) error {
	return func(_ *cobra.Command, args []string) error {
		connection, err := a.connectGRPC(group)
		if err != nil {
			return err
		}
		defer connection.Close()
		ctx, cancel := a.Deadline()
		defer cancel()
		return run(ctx, newClient(connection), args)
	}
}
