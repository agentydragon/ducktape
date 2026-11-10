package main

import (
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/egress/docs"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/egress_rulespb"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
)

func (a *app) egressCommand() *cobra.Command {
	return group("egress", "Read this account's effective egress rules",
		"Calls the Egress rules API (AGENTPLANE_EGRESS_GRPC_URL, AGENTPLANE_EGRESS_GRPC_CREDENTIAL).",
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
			Long: `EgressRules.GetRules: the ServiceAccount (subject) or Identity the credential proves, and the hosts, methods and paths its
egress policies allow. A rule with a credential also says where to send its placeholder, which you must
do yourself: the proxy swaps in the real value only where the placeholder is presented. Select a rule from
the output, for example with jq; the proxy alone decides what matches a call.`,
			Args: cobra.NoArgs,
			RunE: func(_ *cobra.Command, _ []string) error {
				connection, err := a.connectGRPC("EGRESS_GRPC")
				if err != nil {
					return err
				}
				defer connection.Close()
				ctx, cancel := a.deadline()
				defer cancel()
				response, err := egressrulespb.NewEgressRulesClient(connection).GetRules(ctx, &egressrulespb.GetRulesRequest{})
				if err != nil {
					return grpcFailure(ctx, err)
				}
				body, err := (protojson.MarshalOptions{EmitUnpopulated: true}).Marshal(response)
				if err != nil {
					return invalidResponse("%v", err)
				}
				return a.showOne(body, rulesText)
			},
		})
}
