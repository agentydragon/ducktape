package cli

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/api/rules/egress_rulespb"
	"github.com/agentydragon/ducktape/agentplane/egress/docs"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
)

func (a *app) egressCommand() *cobra.Command {
	return invocation.Group("egress", "Read this account's effective egress rules",
		"Calls the Egress rules API (AGENTPLANE_EGRESS_GRPC_URL, AGENTPLANE_EGRESS_GRPC_CREDENTIAL).",
		&cobra.Command{
			Use:   "guide [credentials]",
			Short: "Read the egress workflow guide",
			Args:  cobra.MaximumNArgs(1),
			RunE: func(_ *cobra.Command, args []string) error {
				name := "agents/llms.txt"
				if len(args) == 1 {
					if args[0] != "credentials" {
						return invocation.UsageFailure("unknown egress guide %q", args[0])
					}
					name = "agents/credentials.md"
				}
				content, err := docs.Agents.ReadFile(name)
				if err != nil {
					return err
				}
				_, err = fmt.Fprint(a.Stdout(), string(content))
				return err
			},
		},
		&cobra.Command{
			Use:   "rules",
			Short: "Show the egress rules that apply to this account",
			Long: `EgressRules.GetRules: the ServiceAccount (subject) or Identity the credential proves, and the hosts, methods and paths its
egress policies allow. A rule with a credential also says where to send its placeholder, which you must
do yourself: the proxy swaps in the real value only where the placeholder is presented. Select a rule from
the output, for example with jq; the proxy alone decides what matches a call.`,
			Args: cobra.NoArgs,
			RunE: invocation.GRPCCommand(a.Invocation, "EGRESS_GRPC", egressrulespb.NewEgressRulesClient, func(ctx context.Context, client egressrulespb.EgressRulesClient, _ []string) error {
				response, err := client.GetRules(ctx, &egressrulespb.GetRulesRequest{})
				if err != nil {
					return invocation.GRPCFailure(ctx, err)
				}
				return invocation.ShowProto(a.Invocation, []*egressrulespb.GetRulesResponse{response}, rulesText, protojson.MarshalOptions{EmitUnpopulated: true})
			}),
		})
}
