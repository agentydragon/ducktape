package main

import (
	"encoding/json"
	"errors"
	"fmt"

	actiondocs "github.com/agentydragon/ducktape/agentplane/action_service/docs"
	"github.com/spf13/cobra"
)

func (a *app) actionsGuideCommand() *cobra.Command {
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
				return usageFailure("unknown Action guide %q; run `agentplane actions guide` for the index", name)
			}
			if err != nil {
				return err
			}
			if a.asJSON {
				encoded, err := json.Marshal(guide)
				if err != nil {
					return invalidResponse("marshal Action guide: %v", err)
				}
				return a.showOne(encoded, func(_ json.RawMessage) (string, error) { return guide.Markdown, nil })
			}
			_, err = fmt.Fprint(a.stdout, guide.Markdown)
			return err
		},
	}
}
