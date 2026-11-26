// Package history implements direct CLI calls to the History Service.
package history

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/spf13/cobra"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

type commands struct{ *invocation.Invocation }

// Command returns direct History RPC commands. The backend authorizes every call.
func Command(in *invocation.Invocation) *cobra.Command {
	a := &commands{in}
	return invocation.Group("history", "Read retained Session History", `Calls History Service gRPC methods using
AGENTPLANE_HISTORY_URL and its optional AGENTPLANE_HISTORY_CREDENTIAL. Managed environments use
their workload SPIFFE identity through the local platform route. The service authorizes each read.`,
		a.summaryCommand(), a.eventsCommand())
}

func display[T proto.Message](a *commands, message T) error {
	return invocation.ShowProto(a.Invocation, []T{message}, func(value T) (string, error) {
		data, err := (protojson.MarshalOptions{Multiline: true, Indent: "  "}).Marshal(value)
		return string(data), err
	}, protojson.MarshalOptions{})
}

func (a *commands) historyCommand(run func(context.Context, historypb.HistoryServiceClient, []string) error) func(*cobra.Command, []string) error {
	return invocation.GRPCCommand(a.Invocation, "HISTORY", historypb.NewHistoryServiceClient, run)
}

func (a *commands) summaryCommand() *cobra.Command {
	return &cobra.Command{
		Use: "get-session-summary SESSION_ID", Short: "Read a retained Session summary", Args: cobra.ExactArgs(1),
		RunE: a.historyCommand(func(ctx context.Context, client historypb.HistoryServiceClient, args []string) error {
			response, err := client.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: args[0]})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response == nil {
				return invocation.InvalidResponse("GetSessionSummary returned no response")
			}
			return display(a, response)
		}),
	}
}

func (a *commands) eventsCommand() *cobra.Command {
	var after uint64
	var limit uint32
	command := &cobra.Command{
		Use: "read-session-events SESSION_ID [--after CURSOR] [--limit N]", Short: "Read one page of retained Session events", Args: cobra.ExactArgs(1),
		Long: "Calls HistoryService.ReadSessionEvents. Output includes the backend's entries, removed ranges, settlements, and last cursor.",
		RunE: a.historyCommand(func(ctx context.Context, client historypb.HistoryServiceClient, args []string) error {
			response, err := client.ReadSessionEvents(ctx, &historypb.ReadSessionEventsRequest{SessionId: args[0], AfterCursor: after, Limit: limit})
			if err != nil {
				return invocation.GRPCFailure(ctx, err)
			}
			if response == nil {
				return invocation.InvalidResponse("ReadSessionEvents returned no response")
			}
			return display(a, response)
		}),
	}
	command.Flags().Uint64Var(&after, "after", 0, "exclusive History cursor")
	command.Flags().Uint32Var(&limit, "limit", 100, "maximum events in this page")
	return command
}
