package centralsampler

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/netip"
	"os"
	"regexp"
	"slices"
	"strings"
)

type APIStyle string

const APIStyleResponses APIStyle = "responses"

type ResponsesDialect string

const (
	ResponsesDialectLite     ResponsesDialect = "lite"
	ResponsesDialectStandard ResponsesDialect = "standard"
)

type ExecEncoding string

const (
	ExecEncodingFreeform ExecEncoding = "freeform"
	ExecEncodingJSON     ExecEncoding = "json"
)

// ModelConfig contains the wire and tool-call encodings supported by one model route.
type ModelConfig struct {
	API              APIStyle        `json:"api"`
	Responses        ResponsesConfig `json:"responses"`
	ExecEncoding     ExecEncoding    `json:"exec_encoding"`
	ReasoningEfforts []string        `json:"reasoning_efforts"`
}

type ResponsesConfig struct {
	Dialect ResponsesDialect `json:"dialect"`
}

func (c ModelConfig) Validate() error {
	if len(c.ReasoningEfforts) == 0 {
		return errors.New("model reasoning_efforts must not be empty")
	}
	if c.API != APIStyleResponses {
		return errors.New("model api must be responses")
	}
	if c.Responses.Dialect != ResponsesDialectLite && c.Responses.Dialect != ResponsesDialectStandard {
		return errors.New("responses.dialect must be lite or standard")
	}
	if c.ExecEncoding != ExecEncodingFreeform && c.ExecEncoding != ExecEncodingJSON {
		return errors.New("exec_encoding must be freeform or json")
	}
	return nil
}

type Settings struct {
	Models                          map[string]ModelConfig `json:"models"`
	DatabaseURL                     string                 `json:"database_url"`
	Listen                          string                 `json:"listen"`
	LLMProxyURL                     string                 `json:"llm_proxy_url"`
	Credential                      string                 `json:"credential"`
	CredentialFile                  string                 `json:"credential_file"`
	Audience                        string                 `json:"audience"`
	AllowedAccounts                 []string               `json:"allowed_accounts"`
	CodeModeHost                    string                 `json:"code_mode_host"`
	ExecutorCodex                   string                 `json:"executor_codex"`
	Logs                            string                 `json:"logs"`
	HistoryTarget                   string                 `json:"history_target"`
	HistoryTokenFile                string                 `json:"history_token_file"`
	GatewayTarget                   string                 `json:"gateway_target"`
	GatewayTokenFile                string                 `json:"gateway_token_file"`
	DirectExecutorTarget            string                 `json:"direct_executor_target"`
	DirectExecutorInsecureLocaltest bool                   `json:"direct_executor_insecure_localtest"`
	MaxLiveSessions                 int                    `json:"max_live_sessions"`
	TurnTimeoutSeconds              int                    `json:"turn_timeout_seconds"`
	RPCTimeoutSeconds               int                    `json:"rpc_timeout_seconds"`
	LeaseSeconds                    int                    `json:"lease_seconds"`
	AutoCompactTokens               *int                   `json:"auto_compact_tokens"`
	Kubeconfig                      string                 `json:"kubeconfig"`
}

func LoadSettings(path string) (Settings, error) {
	file, err := os.Open(path)
	if err != nil {
		return Settings{}, err
	}
	defer file.Close()
	settings, err := decodeSettings(file)
	if err != nil {
		return Settings{}, err
	}
	if databaseURL, ok := os.LookupEnv("AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL"); ok {
		settings.DatabaseURL = databaseURL
	}
	return settings, settings.Validate()
}

func DecodeSettings(reader io.Reader) (Settings, error) {
	settings, err := decodeSettings(reader)
	if err != nil {
		return Settings{}, err
	}
	return settings, settings.Validate()
}

func decodeSettings(reader io.Reader) (Settings, error) {
	// Deviation: preserve the sampler's existing strict JSON configuration contract during the Go rewrite.
	decoder := json.NewDecoder(reader)
	decoder.DisallowUnknownFields()
	var settings Settings
	if err := decoder.Decode(&settings); err != nil {
		return Settings{}, err
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return Settings{}, errors.New("configuration must contain exactly one JSON object")
	}
	return settings, nil
}

func (s Settings) Validate() error {
	for name, value := range map[string]string{"database_url": s.DatabaseURL, "listen": s.Listen, "audience": s.Audience, "code_mode_host": s.CodeModeHost, "executor_codex": s.ExecutorCodex, "logs": s.Logs} {
		if value == "" {
			return fmt.Errorf("%s is required", name)
		}
	}
	if len(s.Models) == 0 || len(s.AllowedAccounts) == 0 {
		return errors.New("models and allowed_accounts must not be empty")
	}
	if s.LLMProxyURL == "" || (s.Credential == "") == (s.CredentialFile == "") {
		return errors.New("llm_proxy_url and exactly one credential source are required")
	}
	for model, config := range s.Models {
		if model == "" {
			return errors.New("model route must not be empty")
		}
		if err := config.Validate(); err != nil {
			return fmt.Errorf("model %s: %w", model, err)
		}
	}
	for _, account := range s.AllowedAccounts {
		parts := strings.Split(account, "/")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			return errors.New("allowed_accounts must use namespace/name")
		}
	}
	if (s.HistoryTarget == "") != (s.HistoryTokenFile == "") {
		return errors.New("history target and token file must be configured together")
	}
	gateway := s.GatewayTarget != ""
	if gateway != (s.GatewayTokenFile != "") {
		return errors.New("gateway target and token file must be configured together")
	}
	direct := s.DirectExecutorTarget != ""
	if gateway == direct {
		return errors.New("configure exactly one gateway or direct executor target")
	}
	if direct != s.DirectExecutorInsecureLocaltest {
		return errors.New("direct executor target and explicit insecure localtest mode must be configured together")
	}
	if direct {
		address, err := netip.ParseAddrPort(s.DirectExecutorTarget)
		if err != nil || !address.Addr().IsLoopback() {
			return errors.New("insecure direct executor target must use a loopback IP address")
		}
	}
	remaining := strings.ReplaceAll(strings.ReplaceAll(s.GatewayTarget, "{namespace}", ""), "{sandbox}", "")
	if strings.ContainsAny(remaining, "{}") {
		return errors.New("gateway target supports only namespace and sandbox placeholders")
	}
	if s.MaxLiveSessions <= 0 || s.TurnTimeoutSeconds <= 0 || s.RPCTimeoutSeconds <= 0 || s.LeaseSeconds <= 30 {
		return errors.New("resource limits and timeouts must be positive; lease_seconds must exceed 30")
	}
	if s.AutoCompactTokens != nil && *s.AutoCompactTokens <= 0 {
		return errors.New("auto_compact_tokens must be positive")
	}
	return nil
}

func (s Settings) ReadCredential() (string, error) {
	if s.CredentialFile == "" {
		return s.Credential, nil
	}
	data, err := os.ReadFile(s.CredentialFile)
	if err != nil {
		return "", fmt.Errorf("read LLM proxy credential file: %w", err)
	}
	credential := strings.TrimSpace(string(data))
	if credential == "" {
		return "", errors.New("LLM proxy credential file is empty")
	}
	return credential, nil
}

var executorDNSLabel = regexp.MustCompile(`^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$`)

func (s Settings) GatewayAddress(namespace, sandbox string) (string, error) {
	if s.GatewayTarget == "" {
		return "", errors.New("gateway transport is not configured")
	}
	if strings.Contains(s.GatewayTarget, "{") {
		for _, name := range []string{namespace, sandbox} {
			if len(name) > 63 || !executorDNSLabel.MatchString(name) {
				return "", errors.New("gateway template requires DNS-label executor names")
			}
		}
	}
	return strings.ReplaceAll(strings.ReplaceAll(s.GatewayTarget, "{namespace}", namespace), "{sandbox}", sandbox), nil
}

func (s Settings) AllowsAccount(namespace, name string) bool {
	return slices.Contains(s.AllowedAccounts, namespace+"/"+name)
}
