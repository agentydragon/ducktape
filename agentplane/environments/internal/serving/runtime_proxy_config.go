package serving

import (
	"crypto/x509"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
)

// RuntimeProxyConfig configures the optional Runtime proxy receiver.
type RuntimeProxyConfig struct {
	RunnerSecurity    peertls.Security `mapstructure:"runner_security"`
	Readers           []string         `mapstructure:"readers"`
	SessionCallers    []string         `mapstructure:"session_callers"`
	ControllerCallers []string         `mapstructure:"controller_callers"`
	ExecutorCallers   []string         `mapstructure:"executor_callers"`
}

func (c RuntimeProxyConfig) Validate() error {
	if err := c.RunnerSecurity.Validate(); err != nil {
		return fmt.Errorf("runner_security: %w", err)
	}
	if c.RunnerSecurity.Mode == peertls.MutualTLS && c.RunnerSecurity.TLS.WorkloadAPISocket != "" {
		return errors.New("Runtime proxy runner_security requires file-backed client credentials")
	}
	for _, callers := range [][]string{c.Readers, c.SessionCallers, c.ControllerCallers, c.ExecutorCallers} {
		if err := auth.ValidateServiceCallers(callers); err != nil {
			return err
		}
	}
	return nil
}

// ValidateCredentials requires the dedicated proxy identity for file-backed mTLS.
func (c RuntimeProxyConfig) ValidateCredentials() error {
	if err := c.Validate(); err != nil {
		return err
	}
	if c.RunnerSecurity.Mode != peertls.MutualTLS {
		return nil
	}
	identity, err := c.RunnerSecurity.TLS.LocalIdentity(x509.ExtKeyUsageClientAuth)
	if err != nil {
		return fmt.Errorf("runner_security client identity: %w", err)
	}
	if identity != peertls.RuntimeProxyURI {
		return fmt.Errorf("runner_security client identity must be %q", peertls.RuntimeProxyURI)
	}
	return nil
}
