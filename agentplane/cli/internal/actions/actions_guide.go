package actions

import (
	"errors"

	actiondocs "github.com/agentydragon/ducktape/agentplane/action_service/docs"
	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	"github.com/spf13/cobra"
)

func (a *commands) actionsGuideCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "guide [NAME]",
		Short: "Read a bundled Action workflow guide",
		Long: `Prints the Action Service guide bundled with this CLI image, without making a service call.
Omit NAME for the index. Otherwise use one of: session_delegation, action_lifecycle, recovery, kubernetes.`,
		Args: cobra.MaximumNArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			name := actiondocs.Index
			if len(args) == 1 {
				name = actiondocs.Name(args[0])
			}
			guide, err := actiondocs.Read(name)
			if errors.Is(err, actiondocs.ErrUnknownGuide) {
				return invocation.UsageFailure("unknown Action guide %q; run `agentplane actions guide` for the index", name)
			}
			if err != nil {
				return err
			}
			return a.ShowValue(guide, guide.Markdown)
		},
	}
}
