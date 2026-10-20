package main

import (
	"context"
	"net/http"

	"github.com/spf13/cobra"
)

func (a *app) egressCommand() *cobra.Command {
	return group("egress", "Read this account's effective egress rules",
		"Calls the Egress rules API (AGENTPLANE_EGRESS_URL, AGENTPLANE_EGRESS_CREDENTIAL).",
		&cobra.Command{
			Use:   "rules",
			Short: "Show the egress rules that apply to this account",
			Long: `GET /v1/rules: the ServiceAccount the credential proves (subject), and the hosts, methods, paths and
credential placeholders its egress policies allow. Select a rule from the output, for example with jq;
the proxy alone decides what matches a call.`,
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
