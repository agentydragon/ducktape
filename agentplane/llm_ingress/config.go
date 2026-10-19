package main

import (
	"errors"
	"net/url"
	"os"

	"github.com/spf13/viper"
)

const (
	settingPrefix = "AGENTPLANE_LLM_INGRESS_"
	configFileEnv = settingPrefix + "CONFIG_FILE"
)

type modelConfig struct {
	Model                    string `json:"model" mapstructure:"model"`
	TotalContextBudgetTokens int    `json:"total_context_budget_tokens" mapstructure:"total_context_budget_tokens"`
}

type config struct {
	AllowedServiceAccountNamespaces []string          `mapstructure:"allowed_service_account_namespaces"`
	TokenAudience                   string            `mapstructure:"token_audience"`
	LogLLMRequests                  bool              `mapstructure:"log_llm_requests"`
	Models                          []modelConfig     `mapstructure:"models"`
	LiteLLMURL                      string            `mapstructure:"litellm_url"`
	LiteLLMKey                      string            `mapstructure:"litellm_key"`
	LogLevels                       map[string]string `mapstructure:"log_levels"`
	Host                            string            `mapstructure:"host"`
	Port                            int               `mapstructure:"port"`
	Kubeconfig                      string            `mapstructure:"kubeconfig"`
}

func defaultConfig() config {
	return config{Host: "0.0.0.0", Port: 8080, LogLevels: map[string]string{}}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	settings := viper.New()
	settings.SetEnvPrefix("AGENTPLANE_LLM_INGRESS")
	for _, key := range []string{
		"token_audience", "log_llm_requests", "litellm_url", "litellm_key", "host", "port", "kubeconfig",
	} {
		settings.MustBindEnv(key)
	}
	if path := os.Getenv(configFileEnv); path != "" {
		settings.SetConfigFile(path)
		settings.SetConfigType("yaml")
		if err := settings.ReadInConfig(); err != nil {
			return config{}, errors.New("could not read ingress settings file")
		}
	}
	if err := settings.UnmarshalExact(&c); err != nil {
		return config{}, errors.New("invalid ingress settings")
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
