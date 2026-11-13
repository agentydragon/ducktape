package settings

import (
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
)

type AuthType string

const (
	AuthTypeBearer AuthType = "bearer"
	// AuthTypeAgentplaneIngress is Agentplane's LLM ingress: the credential is the sampler's
	// projected workload token, and a Session executing as an Identity is sampled as a delegating
	// actor for that Identity (providerAuthorization).
	AuthTypeAgentplaneIngress AuthType = "agentplane_ingress"
	AuthTypeAnthropicAPIKey   AuthType = "anthropic_api_key"
)

// ModelConfig contains the wire and tool-call encodings supported by one model route.
type ModelConfig struct {
	Connection        string                `json:"connection"`
	ProviderModel     string                `json:"provider_model"`
	API               protocol.APIStyle     `json:"api"`
	Responses         responses.Config      `json:"responses,omitzero"`
	AnthropicMessages *anthropic.Config     `json:"anthropic_messages,omitempty"`
	ExecEncoding      protocol.ExecEncoding `json:"exec_encoding"`
	ReasoningEfforts  []string              `json:"reasoning_efforts"`
}

func (c ModelConfig) Validate() error {
	if c.Connection == "" {
		return invalidSetting("connection", "model connection is required")
	}
	if c.ProviderModel == "" {
		return invalidSetting("provider_model", "model provider_model is required")
	}
	if len(c.ReasoningEfforts) == 0 {
		return invalidSetting("reasoning_efforts", "model reasoning_efforts must not be empty")
	}
	if c.ExecEncoding != protocol.ExecEncodingFreeform && c.ExecEncoding != protocol.ExecEncodingJSON {
		return invalidSetting("exec_encoding", "exec_encoding must be freeform or json")
	}
	switch c.API {
	case protocol.APIStyleResponses:
		if err := c.Responses.Validate(); err != nil {
			return invalidSettingWithCause("responses", "responses settings are invalid", err)
		}
		if c.AnthropicMessages != nil {
			return invalidSetting("anthropic_messages", "settings are only valid for the anthropic_messages API")
		}
	case protocol.APIStyleAnthropicMessages:
		if c.Responses != (responses.Config{}) {
			return invalidSetting("responses", "settings are only valid for the responses API")
		}
		if c.ExecEncoding != protocol.ExecEncodingJSON {
			return invalidSetting("exec_encoding", "anthropic_messages requires exec_encoding json")
		}
		if c.AnthropicMessages == nil {
			return invalidSetting("anthropic_messages", "settings are required for the anthropic_messages API")
		}
		for _, effort := range c.ReasoningEfforts {
			if !slices.Contains([]string{"low", "medium", "high", "xhigh", "max"}, effort) {
				return invalidSettingWithCause("reasoning_efforts", "anthropic reasoning effort is unsupported", fmt.Errorf("anthropic reasoning effort %q is unsupported", effort))
			}
		}
		if err := c.AnthropicMessages.Validate(); err != nil {
			return invalidSettingWithCause("anthropic_messages", "settings are invalid", err)
		}
	default:
		return invalidSetting("api", "model api must be responses or anthropic_messages")
	}
	return nil
}

func (c ModelConfig) Compatible(other ModelConfig) bool {
	// Matching wire formats do not establish that another model can consume retained
	// signed thinking, encrypted reasoning or compaction items. Keep the provider
	// model fixed until the adapter has an explicit cross-model continuation contract.
	if c.Connection != other.Connection || c.ProviderModel != other.ProviderModel || c.API != other.API || c.ExecEncoding != other.ExecEncoding {
		return false
	}
	switch c.API {
	case protocol.APIStyleResponses:
		return c.Responses.Compatible(other.Responses)
	case protocol.APIStyleAnthropicMessages:
		return c.Responses == other.Responses && c.AnthropicMessages.Compatible(other.AnthropicMessages)
	default:
		return false
	}
}

type ConnectionConfig struct {
	TLS     *environment_runtime_transport.ClientTLS `json:"tls,omitempty"`
	BaseURL string                                   `json:"base_url"`
	Auth    AuthConfig                               `json:"auth"`
}

// HTTPClient retains the provider client's policy while verifying the selected
// service SVID. External providers continue to use the standard HTTPS verifier.
func (c ConnectionConfig) HTTPClient(client *http.Client) (*http.Client, error) {
	if c.TLS == nil {
		return client, nil
	}
	config, err := c.TLS.TLSConfig()
	if err != nil {
		return nil, err
	}
	baseTransport := client.Transport
	if baseTransport == nil {
		baseTransport = http.DefaultTransport
	}
	transport, ok := baseTransport.(*http.Transport)
	if !ok {
		return nil, errors.New("service TLS requires a configurable HTTP transport")
	}
	transport = transport.Clone()
	transport.TLSClientConfig = config
	copy := *client
	copy.Transport = transport
	return &copy, nil
}

type AuthConfig struct {
	Type           AuthType `json:"type"`
	Credential     string   `json:"credential,omitempty"`
	CredentialFile string   `json:"credential_file,omitempty"`
}

func (c ConnectionConfig) Validate() error {
	parsed, err := url.Parse(c.BaseURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" || strings.HasSuffix(parsed.Host, ":") || parsed.User != nil || parsed.RawQuery != "" || parsed.ForceQuery || parsed.Fragment != "" || strings.ContainsAny(c.BaseURL, "?#") {
		return invalidSetting("base_url", "connection base_url must be a safe http(s) URL without credentials, query, or fragment")
	}
	if rawPort := parsed.Port(); rawPort != "" {
		port, err := strconv.Atoi(rawPort)
		if err != nil || port < 1 || port > 65535 {
			return invalidSetting("base_url", "connection base_url must use a valid TCP port")
		}
	}
	if c.TLS != nil {
		if parsed.Scheme != "https" {
			return invalidSetting("tls", "connection tls requires an https base_url")
		}
		if err := c.TLS.Validate(); err != nil {
			return invalidSettingWithCause("tls", "connection TLS settings are invalid", err)
		}
	}
	if (c.Auth.Credential == "") == (c.Auth.CredentialFile == "") {
		return invalidSetting("auth", "connection auth requires exactly one of credential and credential_file")
	}
	switch c.Auth.Type {
	case AuthTypeBearer, AuthTypeAgentplaneIngress, AuthTypeAnthropicAPIKey:
	default:
		return invalidSetting("auth.type", "connection auth.type must be bearer, agentplane_ingress or anthropic_api_key")
	}
	return nil
}

func (a AuthConfig) ReadCredential() (string, error) {
	if (a.Credential == "") == (a.CredentialFile == "") {
		return "", errors.New("connection auth requires exactly one of credential and credential_file")
	}
	if a.CredentialFile == "" {
		return a.Credential, nil
	}
	data, err := os.ReadFile(a.CredentialFile)
	if err != nil {
		return "", fmt.Errorf("read provider credential file: %w", err)
	}
	credential := strings.TrimSpace(string(data))
	if credential == "" {
		return "", errors.New("provider credential file is empty")
	}
	return credential, nil
}
