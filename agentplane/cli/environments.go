package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"strings"

	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	"github.com/spf13/cobra"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

const maxEnvironmentPage = 100

type environmentDiscovery struct {
	connection *grpc.ClientConn
	client     discoverypb.EnvironmentDiscoveryClient
}

func (a *app) connectEnvironmentDiscovery() (*environmentDiscovery, error) {
	const urlVariable, credentialVariable = "AGENTPLANE_SESSIONS_URL", "AGENTPLANE_SESSIONS_CREDENTIAL"
	base, credential := a.getenv(urlVariable), a.getenv(credentialVariable)
	if base == "" || credential == "" {
		missing := urlVariable
		if base != "" {
			missing = credentialVariable
		} else if credential == "" {
			missing += " and " + credentialVariable
		}
		return nil, usageFailure("%s not set", missing)
	}
	parsed, err := parseServiceURL(base, urlVariable)
	if err != nil {
		return nil, err
	}
	if parsed.Path != "" && parsed.Path != "/" {
		return nil, usageFailure("%s must not include a path", urlVariable)
	}
	target, err := grpcTarget(parsed, urlVariable)
	if err != nil {
		return nil, err
	}
	transportCredentials := credentials.TransportCredentials(insecure.NewCredentials())
	if parsed.Scheme == "https" {
		transportCredentials = credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS12, ServerName: parsed.Hostname()})
	}
	connection, err := grpc.NewClient(
		target,
		grpc.WithTransportCredentials(transportCredentials),
		grpc.WithPerRPCCredentials(bearerCredential(credential)),
		grpc.WithDisableRetry(),
	)
	if err != nil {
		return nil, usageFailure("%s is not a valid gRPC endpoint", urlVariable)
	}
	return &environmentDiscovery{connection: connection, client: discoverypb.NewEnvironmentDiscoveryClient(connection)}, nil
}

func (a *app) environmentsCommand() *cobra.Command {
	return group("environments", "Discover existing environments this account may use",
		"Lists and refreshes only exact Sandbox incarnations running as the ServiceAccount proved by AGENTPLANE_SESSIONS_CREDENTIAL.",
		a.listEnvironmentsCommand(), a.getEnvironmentCommand())
}

func (a *app) environmentCommand(fn func(context.Context, discoverypb.EnvironmentDiscoveryClient, []string) error) func(*cobra.Command, []string) error {
	return func(_ *cobra.Command, args []string) error {
		connected, err := a.connectEnvironmentDiscovery()
		if err != nil {
			return err
		}
		defer connected.connection.Close()
		ctx, cancel := a.deadline()
		defer cancel()
		return fn(ctx, connected.client, args)
	}
}

func (a *app) listEnvironmentsCommand() *cobra.Command {
	var limit int
	var pageToken string
	command := &cobra.Command{
		Use:   "list [--limit N] [--after TOKEN]",
		Short: "List one page of usable existing environments",
		Long: `Lists only Sandboxes whose live, controller-owned ServiceAccount UID matches this credential.
Use the complete target namespace/name/UID from a result with environments get. The current
Sandbox-owned account lookup returns no continuation token. Workspace paths and runtime choices are
deployment-configured offers; custom templates without a published mapping remain unavailable.`,
		Args: cobra.NoArgs,
		RunE: a.environmentCommand(func(ctx context.Context, client discoverypb.EnvironmentDiscoveryClient, _ []string) error {
			if limit < 1 || limit > maxEnvironmentPage {
				return usageFailure("--limit must be between 1 and %d", maxEnvironmentPage)
			}
			response, err := client.ListEnvironments(ctx, &discoverypb.ListEnvironmentsRequest{Limit: uint32(limit), PageToken: pageToken})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			return a.showEnvironmentPage(response)
		}),
	}
	command.Flags().IntVar(&limit, "limit", 50, "maximum Sandbox objects inspected (1-100)")
	command.Flags().StringVar(&pageToken, "after", "", "continue with the previous page's next_page_token")
	return command
}

func (a *app) getEnvironmentCommand() *cobra.Command {
	return &cobra.Command{
		Use:   "get NAMESPACE NAME UID",
		Short: "Refresh one exact environment incarnation",
		Long:  "Fetches the current readiness and configured runtime facts only if the Sandbox UID and execution account still match.",
		Args:  cobra.ExactArgs(3),
		RunE: a.environmentCommand(func(ctx context.Context, client discoverypb.EnvironmentDiscoveryClient, args []string) error {
			response, err := client.GetEnvironment(ctx, &discoverypb.GetEnvironmentRequest{
				Target: &discoverypb.TargetHandle{Namespace: args[0], Name: args[1], Uid: args[2]},
			})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			return a.showEnvironment(response)
		}),
	}
}

func (a *app) showEnvironmentPage(response *discoverypb.ListEnvironmentsResponse) error {
	if a.asJSON {
		return a.writeProtoJSON(response)
	}
	for _, environment := range response.GetItems() {
		if _, err := fmt.Fprintln(a.stdout, environmentText(environment)); err != nil {
			return clientFailure(exitRefused, "INTERNAL", "write stdout: %v", err)
		}
	}
	if response.GetNextPageToken() != "" {
		fmt.Fprintf(a.stderr, "more may follow: --after %s\n", response.GetNextPageToken())
	}
	return nil
}

func (a *app) showEnvironment(environment *discoverypb.EnvironmentDescriptor) error {
	if a.asJSON {
		return a.writeProtoJSON(environment)
	}
	if _, err := fmt.Fprintln(a.stdout, environmentText(environment)); err != nil {
		return clientFailure(exitRefused, "INTERNAL", "write stdout: %v", err)
	}
	return nil
}

func (a *app) writeProtoJSON(message proto.Message) error {
	encoded, err := (protojson.MarshalOptions{UseProtoNames: true}).Marshal(message)
	if err != nil {
		return invalidResponse("marshal discovery response: %v", err)
	}
	if _, err := fmt.Fprintln(a.stdout, string(encoded)); err != nil {
		return clientFailure(exitRefused, "INTERNAL", "write stdout: %v", err)
	}
	return nil
}

func environmentText(environment *discoverypb.EnvironmentDescriptor) string {
	var output strings.Builder
	target := environment.GetTarget()
	fmt.Fprintf(&output, "%s/%s uid=%s readiness=%s", target.GetNamespace(), target.GetName(), target.GetUid(), environment.GetReadiness().GetState())
	account := environment.GetExecutionAccount()
	fmt.Fprintf(&output, " account=%s/%s uid=%s", account.GetNamespace(), account.GetName(), account.GetUid())
	fmt.Fprintf(&output, " mode=%s resume=%s wake_on_send=%t", environment.GetOperatingMode(), environment.GetResume(), environment.GetWakeOnSend())
	workspace := environment.GetWorkspace()
	fmt.Fprintf(&output, " workspace=%s", workspace.GetAvailability())
	if workspace.GetAvailability() == discoverypb.Availability_AVAILABILITY_CONFIGURED {
		fmt.Fprintf(&output, " root=%s provenance=%s root_enforced=%t creates_missing_cwd=%t", workspace.GetRoot(), workspace.GetProvenance(), workspace.GetRootEnforced(), workspace.GetCreatesMissingCwd())
	}
	fmt.Fprintf(&output, " configurations=%s", environment.GetConfigurationsAvailability())
	for _, configuration := range environment.GetConfigurations() {
		fmt.Fprintf(&output, "\n  %s %s (%s) efforts=%s", configuration.GetHarness(), configuration.GetModel(), configuration.GetDisplayName(), strings.Join(configuration.GetReasoningEfforts(), ","))
	}
	if readiness := environment.GetReadiness(); readiness.GetReason() != "" || readiness.GetDetail() != "" {
		fmt.Fprintf(&output, "\n  readiness_reason=%s detail=%q", readiness.GetReason(), readiness.GetDetail())
	}
	return output.String()
}
