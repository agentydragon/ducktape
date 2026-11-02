package bridge

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
)

// EnvironmentConfig configures the shared Runner Gateway endpoint and dispatcher-audience token.
type EnvironmentConfig struct {
	Target    string
	TokenFile string
	Timeout   time.Duration
}

// EnvironmentClient is the app's authenticated operator-broker adapter for EnvironmentService.
type EnvironmentClient struct{ config EnvironmentConfig }

// NewEnvironmentClient validates an EnvironmentService transport configuration.
func NewEnvironmentClient(config EnvironmentConfig) (*EnvironmentClient, error) {
	if config.Target == "" || config.TokenFile == "" || config.Timeout <= 0 {
		return nil, errors.New("EnvironmentService target, token file, and positive timeout are required")
	}
	return &EnvironmentClient{config: config}, nil
}

// ResumeEnvironment requests one exact Sandbox resume once. The projected bearer is read for each call.
func (c *EnvironmentClient) ResumeEnvironment(
	ctx context.Context,
	request *environmentpb.ResumeEnvironmentRequest,
) (*environmentpb.ResumeEnvironmentResponse, error) {
	connection, err := bearer.Dial(c.config.Target, c.config.TokenFile, c.config.Timeout)
	if err != nil {
		return nil, err
	}
	defer connection.Close()
	response, err := environmentpb.NewEnvironmentServiceClient(connection).ResumeEnvironment(ctx, request)
	return response, translatedRPCError(err)
}
