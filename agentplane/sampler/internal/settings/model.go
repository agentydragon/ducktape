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
	"sync"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/chatcompletions"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

type AuthType string

const (
	AuthTypeBearer AuthType = "bearer"
	// AuthTypeAgentplaneIngress is Agentplane's LLM ingress: it uses the sampler's projected
	// workload token or central sampler service SVID, and a Session executing as an Identity is
	// sampled as a delegating actor for that Identity (providerAuthorization).
	AuthTypeAgentplaneIngress AuthType = "agentplane_ingress"
	AuthTypeAnthropicAPIKey   AuthType = "anthropic_api_key"
)

// ModelConfig contains the provider settings and sampler policy for one model route.
type ModelConfig struct {
	ResponseHeaderTimeoutSeconds *int                              `json:"response_header_timeout_seconds,omitempty"`
	AutoCompactTokens            *int                              `json:"auto_compact_tokens,omitempty"`
	Connection                   string                            `json:"connection"`
	ProviderModel                string                            `json:"provider_model"`
	API                          protocol.APIStyle                 `json:"api"`
	Responses                    responses.Config                  `json:"responses,omitzero"`
	AnthropicMessages            *anthropic.Config                 `json:"anthropic_messages,omitempty"`
	ChatCompaction               *chatcompletions.CompactionConfig `json:"chat_compaction,omitempty"`
	ExecEncoding                 protocol.ExecEncoding             `json:"exec_encoding"`
	ReasoningEfforts             []string                          `json:"reasoning_efforts"`
}

func (c ModelConfig) Validate() error {
	if c.ResponseHeaderTimeoutSeconds != nil && (*c.ResponseHeaderTimeoutSeconds <= 0 || int64(*c.ResponseHeaderTimeoutSeconds) > maxExecutorInitializeTimeoutSeconds) {
		return invalidSetting("response_header_timeout_seconds", "must be positive and fit in time.Duration")
	}
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
	if c.ChatCompaction != nil {
		if c.API != protocol.APIStyleChatCompletions {
			return invalidSetting("chat_compaction", "requires the chat_completions API")
		}
		if err := c.ChatCompaction.Validate(); err != nil {
			return invalidSettingWithCause("chat_compaction", "settings are invalid", err)
		}
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
	case protocol.APIStyleChatCompletions:
		if c.Responses != (responses.Config{}) || c.AnthropicMessages != nil {
			return invalidSetting("api", "Chat Completions cannot set options for another API")
		}
		if c.ExecEncoding != protocol.ExecEncodingJSON {
			return invalidSetting("exec_encoding", "chat_completions requires exec_encoding json")
		}
	default:
		return invalidSetting("api", "model api must be responses, anthropic_messages, or chat_completions")
	}
	if c.AutoCompactTokens != nil && (*c.AutoCompactTokens <= 0 || !c.SupportsCompaction()) {
		return invalidSetting("auto_compact_tokens", "must be positive and requires compaction support")
	}
	return nil
}

func (c ModelConfig) SupportsCompaction() bool {
	switch c.API {
	case protocol.APIStyleResponses:
		return c.Responses.Dialect == responses.DialectLite
	case protocol.APIStyleAnthropicMessages:
		return c.AnthropicMessages != nil && c.AnthropicMessages.Compaction != nil
	case protocol.APIStyleChatCompletions:
		return c.ChatCompaction != nil
	default:
		return false
	}
}

func (c ModelConfig) Compatible(other ModelConfig) bool {
	// Test whether two routes are aliases for the same provider contract. The RPC
	// separately checks settled conversations eligible for a cross-model switch.
	if c.Connection != other.Connection || c.ProviderModel != other.ProviderModel || c.API != other.API || c.ExecEncoding != other.ExecEncoding {
		return false
	}
	switch c.API {
	case protocol.APIStyleResponses:
		return c.Responses.Compatible(other.Responses)
	case protocol.APIStyleAnthropicMessages:
		return c.Responses == other.Responses && c.AnthropicMessages.Compatible(other.AnthropicMessages)
	case protocol.APIStyleChatCompletions:
		return c.ChatCompaction.Compatible(other.ChatCompaction)
	default:
		return false
	}
}

type ConnectionConfig struct {
	TLS     *peertls.ClientTLS `json:"tls,omitempty"`
	BaseURL string             `json:"base_url"`
	Auth    AuthConfig         `json:"auth"`
}

// httpClient builds the connection's own client and connection pool. A service-SVID connection
// verifies the selected service; external providers use the standard HTTPS verifier. Requests are
// traced and carry W3C trace context to the provider or LLM ingress. The TLS
// config reloads rotated certificates, so the client may be kept for the process lifetime.
func (c ConnectionConfig) httpClient() (*http.Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if c.TLS != nil {
		config, err := c.TLS.TLSConfig()
		if err != nil {
			return nil, err
		}
		transport.TLSClientConfig = config
	}
	return &http.Client{Transport: observability.HTTPTransport(transport)}, nil
}

// ProviderClients holds one HTTP client per configured connection, shared by every Session so
// provider requests reuse keep-alive (and TLS) connections. A client is built on first use; a
// failed build (for example, an SVID not yet mounted) is retried on the next request.
type ProviderClients struct {
	connections map[string]ConnectionConfig
	mu          sync.Mutex
	clients     map[string]*http.Client
}

func NewProviderClients(connections map[string]ConnectionConfig) *ProviderClients {
	return &ProviderClients{connections: connections, clients: map[string]*http.Client{}}
}

// Client returns the shared client for the named connection.
func (p *ProviderClients) Client(name string) (*http.Client, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if client, ok := p.clients[name]; ok {
		return client, nil
	}
	connection, ok := p.connections[name]
	if !ok {
		return nil, fmt.Errorf("unknown connection %s", name)
	}
	client, err := connection.httpClient()
	if err != nil {
		return nil, err
	}
	p.clients[name] = client
	return client, nil
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
	switch c.Auth.Type {
	case AuthTypeBearer, AuthTypeAnthropicAPIKey:
		if (c.Auth.Credential == "") == (c.Auth.CredentialFile == "") {
			return invalidSetting("auth", "connection auth requires exactly one of credential and credential_file")
		}
		if c.TLS != nil && c.TLS.UsesSVID() {
			return invalidSetting("tls", "service SVID authentication is only supported for agentplane_ingress connections")
		}
	case AuthTypeAgentplaneIngress:
		hasCredential := c.Auth.Credential != "" || c.Auth.CredentialFile != ""
		if c.Auth.Credential != "" && c.Auth.CredentialFile != "" {
			return invalidSetting("auth", "connection auth requires at most one of credential and credential_file")
		}
		if c.TLS != nil && c.TLS.ServerURI != peertls.LLMIngressURI {
			return invalidSetting("tls.server_uri", "agentplane_ingress connections must verify the LLM ingress service")
		}
		if c.TLS != nil && c.TLS.UsesSVID() {
			if hasCredential {
				return invalidSetting("auth,tls", "service SVID and bearer credential authentication cannot be combined")
			}
			if err := c.TLS.ValidateCaller("", peertls.CentralSamplerURI); err != nil {
				return invalidSettingWithCause("tls", "connection service SVID caller is invalid", err)
			}
		} else {
			if !hasCredential {
				return invalidSetting("auth", "agentplane_ingress requires a credential or central sampler service SVID")
			}
			if c.TLS != nil {
				if err := c.TLS.ValidateCaller("configured", ""); err != nil {
					return invalidSettingWithCause("tls", "connection TLS settings are invalid", err)
				}
			}
		}
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
