package bridge

import (
	"context"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	lifecyclepb "github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle/lifecyclepb"
)

// LifecycleConfig configures the shared Runner Gateway endpoint and dispatcher-audience token.
type LifecycleConfig struct {
	Target    string
	TokenFile string
	Timeout   time.Duration
}

// LifecycleClient is the app's authenticated operator-broker adapter for EnvironmentLifecycle.
type LifecycleClient struct{ config LifecycleConfig }

// NewLifecycleClient validates an EnvironmentLifecycle transport configuration.
func NewLifecycleClient(config LifecycleConfig) (*LifecycleClient, error) {
	if config.Target == "" || config.TokenFile == "" || config.Timeout <= 0 {
		return nil, errors.New("Environment Lifecycle target, token file, and positive timeout are required")
	}
	return &LifecycleClient{config: config}, nil
}

// ResumeEnvironment requests one exact Sandbox resume once. The projected bearer is read for each call.
func (c *LifecycleClient) ResumeEnvironment(
	ctx context.Context,
	request *lifecyclepb.ResumeEnvironmentRequest,
) (*lifecyclepb.ResumeEnvironmentResponse, error) {
	connection, err := bearer.Dial(c.config.Target, c.config.TokenFile, c.config.Timeout)
	if err != nil {
		return nil, err
	}
	defer connection.Close()
	response, err := lifecyclepb.NewEnvironmentLifecycleClient(connection).ResumeEnvironment(ctx, request)
	return response, translatedRPCError(err)
}
