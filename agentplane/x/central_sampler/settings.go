package centralsampler

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"slices"
	"strings"
)

type Provider struct {
	Endpoint         string   `json:"endpoint"`
	Credential       string   `json:"credential"`
	CredentialFile   string   `json:"credential_file"`
	Models           []string `json:"models"`
	ReasoningEfforts []string `json:"reasoning_efforts"`
}

type Settings struct {
	DatabaseURL        string              `json:"database_url"`
	Listen             string              `json:"listen"`
	ServiceName        string              `json:"service_name"`
	Providers          map[string]Provider `json:"providers"`
	Audience           string              `json:"audience"`
	AllowedAccounts    []string            `json:"allowed_accounts"`
	CodeModeHost       string              `json:"code_mode_host"`
	ExecutorCodex      string              `json:"executor_codex"`
	Logs               string              `json:"logs"`
	HistoryTarget      string              `json:"history_target"`
	HistoryTokenFile   string              `json:"history_token_file"`
	GatewayTarget      string              `json:"gateway_target"`
	GatewayTokenFile   string              `json:"gateway_token_file"`
	DirectExecutorURL  string              `json:"direct_executor_url"`
	MaxLiveSessions    int                 `json:"max_live_sessions"`
	HostRSSBytes       int64               `json:"host_rss_bytes"`
	TurnTimeoutSeconds int                 `json:"turn_timeout_seconds"`
	RPCTimeoutSeconds  int                 `json:"rpc_timeout_seconds"`
	LeaseSeconds       int                 `json:"lease_seconds"`
	AutoCompactTokens  *int                `json:"auto_compact_tokens"`
	Kubeconfig         string              `json:"kubeconfig"`
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
	for name, value := range map[string]string{"database_url": s.DatabaseURL, "listen": s.Listen, "service_name": s.ServiceName, "audience": s.Audience, "code_mode_host": s.CodeModeHost, "executor_codex": s.ExecutorCodex, "logs": s.Logs} {
		if value == "" {
			return fmt.Errorf("%s is required", name)
		}
	}
	if len(s.Providers) == 0 || len(s.AllowedAccounts) == 0 {
		return errors.New("providers and allowed_accounts must not be empty")
	}
	for name, provider := range s.Providers {
		if name == "" || provider.Endpoint == "" || (provider.Credential == "") == (provider.CredentialFile == "") || len(provider.Models) == 0 || len(provider.ReasoningEfforts) == 0 {
			return errors.New("provider endpoint, exactly one credential source, models and reasoning_efforts are required")
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
	if gateway == (s.DirectExecutorURL != "") {
		return errors.New("configure exactly one gateway or direct executor transport")
	}
	remaining := strings.ReplaceAll(strings.ReplaceAll(s.GatewayTarget, "{namespace}", ""), "{sandbox}", "")
	if strings.ContainsAny(remaining, "{}") {
		return errors.New("gateway target supports only namespace and sandbox placeholders")
	}
	if s.MaxLiveSessions <= 0 || s.HostRSSBytes <= 0 || s.TurnTimeoutSeconds <= 0 || s.RPCTimeoutSeconds <= 0 || s.LeaseSeconds <= 30 {
		return errors.New("resource limits and timeouts must be positive; lease_seconds must exceed 30")
	}
	if s.AutoCompactTokens != nil && *s.AutoCompactTokens <= 0 {
		return errors.New("auto_compact_tokens must be positive")
	}
	return nil
}

func (p Provider) ReadCredential() (string, error) {
	if p.CredentialFile == "" {
		return p.Credential, nil
	}
	data, err := os.ReadFile(p.CredentialFile)
	if err != nil {
		return "", fmt.Errorf("read provider credential file: %w", err)
	}
	credential := strings.TrimSpace(string(data))
	if credential == "" {
		return "", errors.New("provider credential file is empty")
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
