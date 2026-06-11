package main

import (
	"fmt"

	suggestionpb "github.com/buildbuddy-io/buildbuddy/proto/suggestion"
	"github.com/spf13/cobra"
)

func askCmd() *cobra.Command {
	var prompt string
	cmd := &cobra.Command{
		Use:   "ask <invocation-id>",
		Short: "Get AI analysis of a build/test failure",
		Args:  cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			c, err := newClient()
			if err != nil {
				return err
			}
			req := &suggestionpb.GetSuggestionRequest{
				Type:         suggestionpb.SuggestionType_FIX_ERROR,
				InvocationId: args[0],
				Prompt:       prompt,
				Service:      suggestionpb.SuggestionService_OPENAI,
			}
			resp := &suggestionpb.GetSuggestionResponse{}
			if err := c.call("GetSuggestion", req, resp); err != nil {
				return err
			}
			if jsonOutput {
				return printProtoJSON(resp)
			}
			for _, s := range resp.GetSuggestion() {
				fmt.Println(s)
			}
			if len(resp.GetSuggestion()) == 0 {
				fmt.Println("No suggestions returned")
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&prompt, "prompt", "", "Custom prompt for the AI analysis")
	return cmd
}
