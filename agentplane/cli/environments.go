package main

import (
	"context"
	"errors"
	"fmt"
	"strings"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/spf13/cobra"
	"google.golang.org/grpc"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

const maxEnvironmentPage = 100

type environmentConnection struct {
	connection *grpc.ClientConn
	client     environmentpb.EnvironmentServiceClient
}

func (a *app) connectEnvironment() (*environmentConnection, error) {
	connection, err := a.connectGRPC("SESSIONS")
	if err != nil {
		return nil, err
	}
	return &environmentConnection{connection: connection, client: environmentpb.NewEnvironmentServiceClient(connection)}, nil
}

func (a *app) environmentsCommand() *cobra.Command {
	return group("environments", "Discover existing environments this account may use",
		"Lists and refreshes exact Sandbox identities (namespace/name/UID) for the authenticated caller. AGENTPLANE_SESSIONS_CREDENTIAL is an optional bearer for forwarders that require it; explicit resume still requires current backend policy.",
		a.listEnvironmentsCommand(), a.getEnvironmentCommand(), a.resumeEnvironmentCommand())
}

func (a *app) resumeEnvironmentCommand() *cobra.Command {
	var resumeToken string
	command := &cobra.Command{
		Use:   "resume NAMESPACE NAME UID --resume-token TOKEN",
		Short: "Request resume of one exact eligible environment",
		Long: `Requests desired running state for the exact existing Sandbox using a fresh opaque token from environments get.
The token binds the request to the Sandbox UID and observed wake generation; the backend rechecks current caller policy.
REQUESTED is acceptance, not readiness. Refresh with environments get to read current readiness before issuing new
work. If the reply is uncertain, refresh the exact target and use only a fresh descriptor's token for another explicit
request. This command never retries the mutation or replays an earlier command.`,
		Args: cobra.ExactArgs(3),
		RunE: func(_ *cobra.Command, args []string) error {
			if resumeToken == "" {
				return usageFailure("--resume-token must not be empty")
			}
			connected, err := a.connectEnvironment()
			if err != nil {
				return err
			}
			defer connected.connection.Close()
			ctx, cancel := a.deadline()
			defer cancel()
			target := &identitypb.EnvironmentTarget{Namespace: args[0], Name: args[1], Uid: args[2]}
			response, err := connected.client.ResumeEnvironment(ctx, &environmentpb.ResumeEnvironmentRequest{
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
	if failure, ok := failureErr.(*failure); ok {
		var detailError error
		if converted, isStatus := status.FromError(err); isStatus {
			for _, detail := range converted.Details() {
				switch typed := detail.(type) {
				case *environmentlifecyclepb.ResumeEligibility:
					encoded, marshalErr := protojson.MarshalOptions{UseProtoNames: true}.Marshal(typed)
					if marshalErr != nil {
						detailError = errors.Join(detailError, fmt.Errorf("encode resume eligibility: %w", marshalErr))
					} else {
						failure.ResumeEligibility = encoded
					}
				case *environmentlifecyclepb.WakeAssessment:
					encoded, marshalErr := protojson.MarshalOptions{UseProtoNames: true}.Marshal(typed)
					if marshalErr != nil {
						detailError = errors.Join(detailError, fmt.Errorf("encode wake assessment: %w", marshalErr))
					} else {
						failure.WakeAssessment = encoded
					}
					if typed.GetState() == environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN {
						failure.exit = exitExecutionUnknown
						failure.Message = "resume outcome is unknown; refresh the exact environment and use only its current resume token for any new explicit request"
					}
				}
			}
		}
		if failure.ambiguous() {
			failure.exit = exitExecutionUnknown
			failure.Message = "resume outcome is unknown; refresh the exact environment and use only its current resume token for any new explicit request"
		}
		if detailError != nil {
			failure.Message += "; " + detailError.Error()
		}
	}
	return failureErr
}

func (a *app) environmentCommand(fn func(context.Context, environmentpb.EnvironmentServiceClient, []string) error) func(*cobra.Command, []string) error {
	return func(_ *cobra.Command, args []string) error {
		connected, err := a.connectEnvironment()
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
		Long: `Lists only Sandboxes whose live, controller-owned ServiceAccount UID matches the authenticated caller.
Use the complete target namespace/name/UID from a result with environments get. The current
Sandbox-owned account lookup returns no continuation token. Workspace paths and runtime choices are
deployment-configured offers; custom templates without a published mapping remain unavailable.`,
		Args: cobra.NoArgs,
		RunE: a.environmentCommand(func(ctx context.Context, client environmentpb.EnvironmentServiceClient, _ []string) error {
			if limit < 1 || limit > maxEnvironmentPage {
				return usageFailure("--limit must be between 1 and %d", maxEnvironmentPage)
			}
			response, err := client.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{Limit: uint32(limit), PageToken: pageToken})
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
		Short: "Refresh one exact Sandbox identity",
		Long:  "Fetch current readiness, explicit-resume eligibility, token and runtime facts for the exact Sandbox UID. This read never resumes the environment.",
		Args:  cobra.ExactArgs(3),
		RunE: a.environmentCommand(func(ctx context.Context, client environmentpb.EnvironmentServiceClient, args []string) error {
			response, err := client.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{
				Target: &identitypb.EnvironmentTarget{Namespace: args[0], Name: args[1], Uid: args[2]},
			})
			if err != nil {
				return grpcFailure(ctx, err)
			}
			return a.showEnvironment(response)
		}),
	}
}

func (a *app) showEnvironmentPage(response *environmentpb.ListEnvironmentsResponse) error {
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

func (a *app) showEnvironment(environment *environmentpb.EnvironmentDescriptor) error {
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

func environmentText(environment *environmentpb.EnvironmentDescriptor) string {
	var output strings.Builder
	target := environment.GetTarget()
	configuration := environment.GetConfiguration()
	state := environment.GetObservedState()
	actions := environment.GetActions()
	fmt.Fprintf(&output, "%s/%s uid=%s readiness=%s", target.GetNamespace(), target.GetName(), target.GetUid(), state.GetReadiness().GetState())
	account := environment.GetExecutionAccount()
	fmt.Fprintf(&output, " account=%s/%s uid=%s", account.GetAccount().GetNamespace(), account.GetAccount().GetName(), account.GetUid())
	fmt.Fprintf(&output, " mode=%s automatic_wake=%s wake_on_send=%t", state.GetOperatingMode(), actions.GetAutomaticWake(), configuration.GetWakeOnSend())
	eligibility := actions.GetResumeEligibility()
	fmt.Fprintf(&output, " explicit_resume=%s reason=%s wake_generation=%d", eligibility.GetState(), eligibility.GetReason(), actions.GetWakeGeneration())
	if actions.GetResumeToken() != "" {
		fmt.Fprintf(&output, "\n  resume_token=%s", actions.GetResumeToken())
	}
	workspace := configuration.GetWorkspace()
	fmt.Fprintf(&output, " workspace=%s", workspace.GetAvailability())
	if workspace.GetAvailability() == environmentpb.Availability_AVAILABILITY_CONFIGURED {
		fmt.Fprintf(&output, " root=%s provenance=%s root_enforced=%t creates_missing_cwd=%t", workspace.GetRoot(), workspace.GetProvenance(), workspace.GetRootEnforced(), workspace.GetCreatesMissingCwd())
	}
	fmt.Fprintf(&output, " configurations=%s", configuration.GetConfigurationsAvailability())
	for _, runtime := range configuration.GetConfigurations() {
		fmt.Fprintf(&output, "\n  %s %s (%s) efforts=%s", runtime.GetHarness(), runtime.GetModel(), runtime.GetDisplayName(), strings.Join(runtime.GetReasoningEfforts(), ","))
	}
	if readiness := state.GetReadiness(); readiness.GetReason() != "" || readiness.GetDetail() != "" {
		fmt.Fprintf(&output, "\n  readiness_reason=%s detail=%q", readiness.GetReason(), readiness.GetDetail())
	}
	return output.String()
}

func environmentResumeText(response *environmentpb.ResumeEnvironmentResponse) string {
	target := response.GetTarget()
	line := fmt.Sprintf("%s/%s uid=%s outcome=%s", target.GetNamespace(), target.GetName(), target.GetUid(), response.GetOutcome())
	if response.WakeGeneration != nil {
		line += fmt.Sprintf(" wake_generation=%d", response.GetWakeGeneration())
	}
	return line + " ready=not-established"
}
