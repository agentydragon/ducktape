package configuration

import (
	"bytes"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	runnertransport "github.com/agentydragon/ducktape/agentplane/runner_transport"
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

// RunnerControlConfiguration exposes the Runner as a Gateway-dialed gRPC server.
type RunnerControlConfiguration struct {
	ListenAddress string                   `json:"listen_address"`
	Security      runnertransport.Security `json:"security"`
}

type Resolved struct {
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
}

func ValidateControlIdentity(control RunnerControlConfiguration, sandbox SandboxIdentity) error {
	if err := control.Security.Validate(); err != nil {
		return fmt.Errorf("Runner control security: %w", err)
	}
	if control.Security.Mode == runnertransport.Insecure {
		return nil
	}
	identity, err := control.Security.TLS.LocalIdentity(x509.ExtKeyUsageServerAuth)
	if err != nil {
		return fmt.Errorf("Runner control certificate: %w", err)
	}
	namespace, sandboxUID, _, err := runnertransport.ParseRunnerURI(identity)
	if err != nil {
		return fmt.Errorf("Runner control certificate: %w", err)
	}
	if namespace != sandbox.Namespace || sandboxUID != sandbox.UID {
		return errors.New("Runner control certificate does not name the configured Sandbox namespace and UID")
	}
	return nil
}

func decodeStrict(data []byte, value any) error {
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
