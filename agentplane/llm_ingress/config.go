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

type modelConfig struct {
	Model                    string `json:"model" yaml:"model"`
	TotalContextBudgetTokens int    `json:"total_context_budget_tokens" yaml:"total_context_budget_tokens"`
}

type config struct {
	AllowedServiceAccountNamespaces []string          `yaml:"allowed_service_account_namespaces"`
	TokenAudience                   string            `yaml:"token_audience"`
	LogLLMRequests                  bool              `yaml:"log_llm_requests"`
	Models                          []modelConfig     `yaml:"models"`
	LiteLLMURL                      string            `env:"LITELLM_URL" yaml:"litellm_url"`
	LiteLLMKey                      string            `env:"LITELLM_KEY" yaml:"litellm_key"`
	LogLevels                       map[string]string `yaml:"log_levels"`
	Host                            string            `yaml:"host"`
	Port                            int               `yaml:"port"`
	Kubeconfig                      string            `yaml:"kubeconfig"`
}

func defaultConfig() config {
	return config{Host: "0.0.0.0", Port: 8080, LogLevels: map[string]string{}}
}

func loadConfig(environ []string) (config, error) {
	c, err := settings.Load(settings.Options{Prefix: settingPrefix, CheckFile: validateYAMLShapes}, defaultConfig(), environ)
	if err != nil {
		return config{}, err
	}
	if err := validateConfig(&c); err != nil {
		return config{}, err
	}
	return c, nil
}

func validateYAMLShapes(contents []byte) error {
	var document yaml.Node
	if err := yaml.Unmarshal(contents, &document); err != nil {
		return err
	}
	if len(document.Content) == 0 {
		return nil
	}
	root := document.Content[0]
	if root.Kind != yaml.MappingNode {
		return errors.New("settings must be a mapping")
	}
	for index := 0; index < len(root.Content); index += 2 {
		name, value := root.Content[index].Value, root.Content[index+1]
		switch name {
		case "allowed_service_account_namespaces":
			if value.Kind != yaml.SequenceNode {
				return errors.New("allowed namespaces must be a list")
			}
		case "log_levels":
			if value.Kind != yaml.MappingNode {
				return errors.New("log_levels must be a mapping")
			}
		case "models":
			if value.Kind != yaml.SequenceNode {
				return errors.New("models must be a list")
			}
			for _, model := range value.Content {
				if model.Kind != yaml.MappingNode {
					return errors.New("models must contain mappings")
				}
				for modelIndex := 0; modelIndex < len(model.Content); modelIndex += 2 {
					if model.Content[modelIndex].Value == "total_context_budget_tokens" && model.Content[modelIndex+1].Tag != "!!int" {
						return errors.New("context budget must be an integer")
					}
				}
			}
		}
	}
	return nil
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
