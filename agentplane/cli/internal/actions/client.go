package actions

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/spf13/cobra"
	"google.golang.org/grpc"
)

// actionService is a generated gRPC client of the Action Service. Sandbox endpoints use the
// credential-holding sidecar's local gRPC route. An explicitly configured credential is sent as
// Authorization metadata. grpc-go uses HTTPS_PROXY and CONNECT even for an http origin.
type actionService struct {
	connection *grpc.ClientConn
	client     action_servicepb.ActionServiceClient
}

func (a *commands) connectActions() (*actionService, error) {
	connection, err := a.ConnectGRPC("ACTIONS")
	if err != nil {
		return nil, err
	}
	return &actionService{connection: connection, client: action_servicepb.NewActionServiceClient(connection)}, nil
}

type actionCall func(context.Context, action_servicepb.ActionServiceClient, []string) error

func (a *commands) actionCommand(fn actionCall) func(*cobra.Command, []string) error {
	return func(_ *cobra.Command, args []string) error {
		connected, err := a.connectActions()
		if err != nil {
			return err
		}
		defer connected.connection.Close()
		ctx, cancel := a.Deadline()
		defer cancel()
		return fn(ctx, connected.client, args)
	}
}
