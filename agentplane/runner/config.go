// Package runner owns durable Sessions and supervises their native harnesses.
package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	runnertransport "github.com/agentydragon/ducktape/agentplane/runner_transport"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

type SandboxIdentity struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	UID       string `json:"uid"`
}

type SandboxLaunch struct {
	Sandbox              SandboxIdentity `json:"sandbox"`
	Bootstrap            *string         `json:"bootstrap"`
	PlatformInstructions string          `json:"-"`
}

// Destination is the JSON notification destination of one Session: the Sandbox and the Session ID.
func (l SandboxLaunch) Destination(sessionID string) string {
	destination, _ := json.Marshal(struct {
		Destination SandboxIdentity `json:"destination_ref"`
		SessionID   string          `json:"session_id"`
	}{l.Sandbox, sessionID})
	return string(destination)
}

func (l SandboxLaunch) Instructions(sessionID, task string) string {
	parts := []string{}
	for _, part := range []string{l.PlatformInstructions, "Your explicit notification destination is:\n" + l.Destination(sessionID) + "\nUse these identifiers when addressing this session; they are not credentials.", task} {
		if part = strings.TrimSpace(part); part != "" {
			parts = append(parts, part)
		}
	}
	return strings.Join(parts, "\n\n")
}

type Endpoint struct {
	Target string `json:"target"`
	Bearer string `json:"bearer"`
}

// RunnerControlConfiguration is a required, explicit transport selection. Only one of Target or
// ListenAddress is valid for the selected mode; every mode uses certificate authentication.
type RunnerControlConfiguration struct {
	Mode          string                 `json:"mode"`
	Target        string                 `json:"target"`
	ListenAddress string                 `json:"listen_address"`
	TLS           runnertransport.Config `json:"tls"`
}

type Config struct {
	Control                          RunnerControlConfiguration
	StateDir                         string
	SessionReconcileConcurrency      int
	Launch                           SandboxLaunch
	HarnessEnvironment               map[string]string
	Claude, Codex                    *harness.LaunchConfig
	VMProcessPolicy                  *supervision.SystemdPolicy
	PodPID1                          bool
	CodexExecServer                  *codexexecserver.Config
	WorkspaceRoot, InitializationCwd string
	DebugCheckpoint                  *session.DebugCheckpointConfig
	HTTPClient                       *http.Client
}

func validateControlIdentity(config Config) error {
	if config.Control.Mode == "" {
		return nil // Unit tests may construct a Runner without the executable's file configuration.
	}
	identity, err := config.Control.TLS.LocalIdentity()
	if err != nil {
		return fmt.Errorf("Runner control certificate: %w", err)
	}
	namespace, sandboxUID, _, err := runnertransport.ParseRunnerURI(identity)
	if err != nil {
		return fmt.Errorf("Runner control certificate: %w", err)
	}
	if namespace != config.Launch.Sandbox.Namespace || sandboxUID != config.Launch.Sandbox.UID {
		return errors.New("Runner control certificate does not name the configured Sandbox namespace and UID")
	}
	return nil
}

func (c *Config) resolveSessionReconcileConcurrency() error {
	if c.SessionReconcileConcurrency < 0 {
		return errors.New("session reconcile concurrency must be positive")
	}
	if c.SessionReconcileConcurrency == 0 {
		c.SessionReconcileConcurrency = defaultSessionReconcileConcurrency
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

func DecodeStrict(data []byte, value any) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(value); err != nil {
		return err
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return errors.New("unexpected trailing JSON data")
	}
	return nil
}
