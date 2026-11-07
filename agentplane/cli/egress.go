package main

import (
	"context"
	"fmt"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/egress/docs"

	"github.com/spf13/cobra"
)

func (a *app) egressCommand() *cobra.Command {
	return group("egress", "Read this account's effective egress rules",
		"Calls the Egress rules API (AGENTPLANE_EGRESS_URL, AGENTPLANE_EGRESS_CREDENTIAL).",
		&cobra.Command{
			Use:   "guide [credentials]",
			Short: "Read the egress workflow guide",
			Args:  cobra.MaximumNArgs(1),
			RunE: func(_ *cobra.Command, args []string) error {
				name := "agents/llms.txt"
				if len(args) == 1 {
					if args[0] != "credentials" {
						return usageFailure("unknown egress guide %q", args[0])
					}
					name = "agents/credentials.md"
				}
				content, err := docs.Agents.ReadFile(name)
				if err != nil {
					return err
				}
				_, err = fmt.Fprint(a.stdout, string(content))
				return err
			},
		},
		&cobra.Command{
			Use:   "rules",
			Short: "Show the egress rules that apply to this account",
			Long: `GET /v1/rules: the ServiceAccount the credential proves (subject), and the hosts, methods and paths its
egress policies allow. A rule with a credential also says where to send its placeholder, which you must
do yourself: the proxy swaps in the real value only where the placeholder is presented. Select a rule from
the output, for example with jq; the proxy alone decides what matches a call.`,
			Args: cobra.NoArgs,
			RunE: a.command("EGRESS", func(ctx context.Context, svc *service, _ []string) error {
				body, err := svc.do(ctx, request{method: http.MethodGet, path: "/v1/rules"})
				if err != nil {
					return err
				}
				return a.showOne(body, rulesText)
			}),
		})
}
