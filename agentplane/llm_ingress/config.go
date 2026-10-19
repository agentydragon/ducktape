package main

import (
	"errors"
	"net/url"

	"github.com/agentydragon/ducktape/util/settings"
	"gopkg.in/yaml.v3"
)

const (
	settingPrefix = "AGENTPLANE_LLM_INGRESS_"
	configFileEnv = settingPrefix + "CONFIG_FILE"
)

// modelBudget rejects YAML's float-to-int coercion for context limits.
type modelBudget int

func (budget *modelBudget) UnmarshalYAML(node *yaml.Node) error {
	if node.Kind != yaml.ScalarNode || node.ShortTag() != "!!int" {
		return errors.New("context budget must be an integer")
	}
	return node.Decode((*int)(budget))
}

type modelConfig struct {
	Model                    string      `json:"model" yaml:"model"`
	TotalContextBudgetTokens modelBudget `json:"total_context_budget_tokens" yaml:"total_context_budget_tokens"`
}

type config struct {
	AllowedServiceAccountNamespaces []string          `env:"-" yaml:"allowed_service_account_namespaces"`
	TokenAudience                   string            `yaml:"token_audience"`
	LogLLMRequests                  bool              `yaml:"log_llm_requests"`
	Models                          []modelConfig     `env:"-" yaml:"models"`
	LiteLLMURL                      string            `env:"LITELLM_URL" yaml:"litellm_url"`
	LiteLLMKey                      string            `env:"LITELLM_KEY" yaml:"litellm_key"`
	LogLevels                       map[string]string `env:"-" yaml:"log_levels"`
	Host                            string            `yaml:"host"`
	Port                            int               `yaml:"port"`
	Kubeconfig                      string            `yaml:"kubeconfig"`
}

func defaultConfig() config {
	return config{Host: "0.0.0.0", Port: 8080, LogLevels: map[string]string{}}
}

func loadConfig(environ []string) (config, error) {
	c, err := settings.Load(settingPrefix, defaultConfig(), environ)
	if err != nil {
		return config{}, err
	}
	if err := validateConfig(&c); err != nil {
		return config{}, err
	}
	return c, nil
}

func validateConfig(c *config) error {
	if len(c.AllowedServiceAccountNamespaces) == 0 {
		return errors.New("allowed_service_account_namespaces must be non-empty")
	}
	for _, namespace := range c.AllowedServiceAccountNamespaces {
		if namespace == "" {
			return errors.New("allowed_service_account_namespaces cannot contain an empty namespace")
		}
	}
	if c.TokenAudience == "" {
		return errors.New("token_audience is required")
	}
	backend, err := url.Parse(c.LiteLLMURL)
	if err != nil || (backend.Scheme != "http" && backend.Scheme != "https") || backend.Hostname() == "" ||
		backend.User != nil || backend.RawQuery != "" || backend.Fragment != "" {
		return errors.New("litellm_url must be an http(s) URL without credentials, query, or fragment")
	}
	if c.LiteLLMKey == "" {
		return errors.New("litellm_key is required")
	}
	if c.Host == "" || c.Port < 0 || c.Port > 65535 {
		return errors.New("host must be non-empty and port must be between 0 and 65535")
	}
	for _, model := range c.Models {
		if model.Model == "" || model.TotalContextBudgetTokens <= 0 {
			return errors.New("models require a non-empty model and positive total_context_budget_tokens")
		}
	}
	for _, level := range c.LogLevels {
		switch level {
		case "DEBUG", "INFO", "WARNING", "ERROR":
		default:
			return errors.New("log_levels values must be DEBUG, INFO, WARNING, or ERROR")
		}
	}
	return nil
}
