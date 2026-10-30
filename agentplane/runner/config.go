// Package runner owns durable Sessions and supervises their native harnesses.
package runner

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/runner/configuration"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
)

type Config struct {
	Control                          configuration.RunnerControlConfiguration
	StateDir                         string
	SessionReconcileConcurrency      int
	Launch                           configuration.SandboxLaunch
	HarnessEnvironment               map[string]string
	Claude, Codex                    *harness.LaunchConfig
	VMProcessPolicy                  *supervision.SystemdPolicy
	PodPID1                          bool
	CodexExecServer                  *codexexecserver.Config
	CodexAppServerReadiness          codexappserver.Readiness
	WorkspaceRoot, InitializationCwd string
	DebugCheckpoint                  *session.DebugCheckpointConfig
	HTTPClient                       *http.Client
}

func (c *Config) resolveSessionReconcileConcurrency() error {
	if c.SessionReconcileConcurrency < 0 {
		return errors.New("session reconcile concurrency must be positive")
	}
	if c.SessionReconcileConcurrency == 0 {
		c.SessionReconcileConcurrency = configuration.DefaultSessionReconcileConcurrency
	}
	return nil
}

// ResolveModelConfig returns the context budget the LLM ingress configures for `model`, or nil for none.
// Codex has no budget here: it reads its context window from the model catalog its provider names.
func (c Config) ResolveModelConfig(ctx context.Context, harness runnerpb.Harness, model string) (*int, error) {
	launch := c.Claude
	if harness == runnerpb.Harness_HARNESS_CODEX {
		launch = c.Codex
	}
	if launch == nil {
		return nil, errors.New("the selected harness is not configured on this runner")
	}
	if harness == runnerpb.Harness_HARNESS_CODEX {
		return nil, nil
	}
	u, err := launch.PlatformURL("/agentplane/model-config")
	if err != nil {
		return nil, err
	}
	q := url.Values{}
	q.Set("model", model)
	u.RawQuery = q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+launch.Token)
	client := http.Client{Timeout: 10 * time.Second}
	if c.HTTPClient != nil {
		client = *c.HTTPClient
	}
	client.Transport = observability.HTTPTransport(client.Transport)
	res, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("could not query the LLM proxy for model configuration: %w", err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("LLM proxy model-config lookup failed with HTTP %d", res.StatusCode)
	}
	var payload *struct {
		Model  string `json:"model"`
		Budget int    `json:"total_context_budget_tokens"`
	}
	decoder := json.NewDecoder(io.LimitReader(res.Body, 65537))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&payload); err != nil {
		return nil, fmt.Errorf("LLM proxy returned malformed model configuration: %w", err)
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, errors.New("LLM proxy returned trailing model configuration data")
	}
	if payload == nil {
		return nil, nil
	}
	if payload.Model != model || payload.Budget <= 0 {
		return nil, errors.New("LLM proxy returned invalid model configuration")
	}
	return &payload.Budget, nil
}
