package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"strings"

	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	lifecyclepb "github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle/lifecyclepb"
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
	lifecycle  lifecyclepb.EnvironmentLifecycleClient
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
	return &environmentDiscovery{
		connection: connection,
		client:     discoverypb.NewEnvironmentDiscoveryClient(connection),
		lifecycle:  lifecyclepb.NewEnvironmentLifecycleClient(connection),
	}, nil
}

func (a *app) environmentsCommand() *cobra.Command {
	return group("environments", "Discover existing environments this account may use",
		"Lists and refreshes exact Sandbox incarnations for the ServiceAccount proved by AGENTPLANE_SESSIONS_CREDENTIAL. Explicit resume still requires current backend policy.",
		a.listEnvironmentsCommand(), a.getEnvironmentCommand(), a.resumeEnvironmentCommand())
}

func (a *app) resumeEnvironmentCommand() *cobra.Command {
	var resumeToken string
	command := &cobra.Command{
		Use:   "resume NAMESPACE NAME UID --resume-token TOKEN",
		Short: "Request resume of one exact eligible environment",
		Long: `Requests desired running state for the exact existing Sandbox using a fresh opaque token from environments get.
The token binds the request to the Sandbox UID and observed wake generation; the backend rechecks current caller policy.
REQUESTED is acceptance, not readiness. Read or wait with environments get before issuing new work. If the reply is
uncertain, refresh the exact target and use only a fresh descriptor's token for another explicit request. This
command never retries the mutation or replays an earlier command.`,
		Args: cobra.ExactArgs(3),
		RunE: func(_ *cobra.Command, args []string) error {
			if resumeToken == "" {
				return usageFailure("--resume-token must not be empty")
			}
			connected, err := a.connectEnvironmentDiscovery()
			if err != nil {
				return err
			}
			defer connected.connection.Close()
			ctx, cancel := a.deadline()
			defer cancel()
			target := &discoverypb.TargetHandle{Namespace: args[0], Name: args[1], Uid: args[2]}
			response, err := connected.lifecycle.ResumeEnvironment(ctx, &lifecyclepb.ResumeEnvironmentRequest{
				Target:      target,
				ResumeToken: resumeToken,
			})
			if err != nil {
				return resumeFailure(ctx, err)
			}
			if response == nil || response.GetTarget() == nil || response.GetTarget().GetNamespace() != target.GetNamespace() ||
				response.GetTarget().GetName() != target.GetName() || response.GetTarget().GetUid() != target.GetUid() {
				return invalidResponse("ResumeEnvironment returned a different target")
			}
			if a.asJSON {
				return a.writeProtoJSON(response)
			}
			if _, err := fmt.Fprintln(a.stdout, environmentResumeText(response)); err != nil {
				return clientFailure(exitRefused, "INTERNAL", "write stdout: %v", err)
			}
			return nil
		},
	}
	command.Flags().StringVar(&resumeToken, "resume-token", "", "opaque precondition returned by environments get for this exact Sandbox generation")
	_ = command.MarkFlagRequired("resume-token")
	return command
}

func resumeFailure(ctx context.Context, err error) error {
	failureErr := grpcFailure(ctx, err)
	if failure, ok := failureErr.(*failure); ok && failure.ambiguous() {
		failure.exit = exitExecutionUnknown
		failure.Message = "resume outcome is unknown; refresh the exact environment and use only its current resume token for any new explicit request"
	}
	return failureErr
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
	var waitSeconds int
	command := &cobra.Command{
		Use:   "get NAMESPACE NAME UID",
		Short: "Refresh one exact environment incarnation",
		Long:  "Fetch current readiness, explicit-resume eligibility, token and runtime facts for the exact Sandbox UID. Optionally wait up to 30 seconds for readiness; if it is still not ready, the latest observed facts are returned. This read never resumes the environment. Set the global --timeout above --wait-seconds to receive the final descriptor.",
		Args:  cobra.ExactArgs(3),
		RunE: func(command *cobra.Command, args []string) error {
			if waitSeconds < 0 || waitSeconds > 30 {
				return usageFailure("--wait-seconds must be between 0 and 30")
			}
			return a.environmentCommand(func(ctx context.Context, client discoverypb.EnvironmentDiscoveryClient, args []string) error {
				response, err := client.GetEnvironment(ctx, &discoverypb.GetEnvironmentRequest{
					Target:              &discoverypb.TargetHandle{Namespace: args[0], Name: args[1], Uid: args[2]},
					WaitForReadySeconds: uint32(waitSeconds),
				})
				if err != nil {
					return grpcFailure(ctx, err)
				}
				return a.showEnvironment(response)
			})(command, args)
		},
	}
	command.Flags().IntVar(&waitSeconds, "wait-seconds", 0, "wait up to this many seconds for readiness (0-30), then return current facts")
	return command
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
	fmt.Fprintf(&output, " mode=%s wake_on_send_resume=%s wake_on_send=%t", environment.GetOperatingMode(), environment.GetResume(), environment.GetWakeOnSend())
	eligibility := environment.GetResumeEligibility()
	fmt.Fprintf(&output, " explicit_resume=%s reason=%s wake_generation=%d", eligibility.GetState(), eligibility.GetReason(), environment.GetWakeGeneration())
	if environment.GetResumeToken() != "" {
		fmt.Fprintf(&output, "\n  resume_token=%s", environment.GetResumeToken())
	}
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

func environmentResumeText(response *lifecyclepb.ResumeEnvironmentResponse) string {
	target := response.GetTarget()
	line := fmt.Sprintf("%s/%s uid=%s outcome=%s", target.GetNamespace(), target.GetName(), target.GetUid(), response.GetOutcome())
	if response.WakeGeneration != nil {
		line += fmt.Sprintf(" wake_generation=%d", response.GetWakeGeneration())
	}
	return line + " ready=not-established"
}
