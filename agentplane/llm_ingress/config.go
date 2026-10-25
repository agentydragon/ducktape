package main

import (
	"errors"
	"net/url"
	"os"
	"regexp"
	"strings"

	"github.com/spf13/viper"
	"go.opentelemetry.io/otel/baggage"
)

const (
	settingPrefix = "AGENTPLANE_LLM_INGRESS_"
	configFileEnv = settingPrefix + "CONFIG_FILE"
)

type verifiedClaim string

const (
	verifiedNamespace      verifiedClaim = "namespace"
	verifiedServiceAccount verifiedClaim = "service_account"
	verifiedPodName        verifiedClaim = "pod_name"
	verifiedPodUID         verifiedClaim = "pod_uid"
)

type attributionField struct {
	Key    string        `mapstructure:"key"`
	Source verifiedClaim `mapstructure:"source"`
}

type attributionJSONHeader struct {
	Name   string             `mapstructure:"name"`
	Fields []attributionField `mapstructure:"fields"`
}

type attributionConfig struct {
	JSONHeaders []attributionJSONHeader `mapstructure:"json_headers"`
	Baggage     []attributionField      `mapstructure:"baggage"`
}

func defaultAttribution() attributionConfig {
	return attributionConfig{JSONHeaders: []attributionJSONHeader{{
		Name: "x-litellm-spend-logs-metadata",
		Fields: []attributionField{
			{Key: "agentplane.namespace", Source: verifiedNamespace},
			{Key: "agentplane.service_account", Source: verifiedServiceAccount},
			{Key: "agentplane.pod_name", Source: verifiedPodName},
			{Key: "agentplane.pod_uid", Source: verifiedPodUID},
		},
	}}}
}

type modelConfig struct {
	Model                    string `json:"model" mapstructure:"model"`
	TotalContextBudgetTokens int    `json:"total_context_budget_tokens" mapstructure:"total_context_budget_tokens"`
}

type config struct {
	AllowedServiceAccountNamespaces []string          `mapstructure:"allowed_service_account_namespaces"`
	TokenAudience                   string            `mapstructure:"token_audience"`
	LogLLMRequests                  bool              `mapstructure:"log_llm_requests"`
	Models                          []modelConfig     `mapstructure:"models"`
	Attribution                     attributionConfig `mapstructure:"attribution"`
	CodexModelCatalogFile           string            `mapstructure:"codex_model_catalog_file"`
	LiteLLMURL                      string            `mapstructure:"litellm_url"`
	LiteLLMKey                      string            `mapstructure:"litellm_key"`
	LogLevels                       map[string]string `mapstructure:"log_levels"`
	Host                            string            `mapstructure:"host"`
	Port                            int               `mapstructure:"port"`
	Kubeconfig                      string            `mapstructure:"kubeconfig"`
}

func defaultConfig() config {
	return config{
		Host:      "0.0.0.0",
		Port:      8080,
		LogLevels: map[string]string{},
	}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	settings := viper.New()
	settings.SetEnvPrefix("AGENTPLANE_LLM_INGRESS")
	for _, key := range []string{
		"token_audience", "log_llm_requests", "codex_model_catalog_file", "litellm_url", "litellm_key", "host", "port",
		"kubeconfig",
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
	if !settings.InConfig("attribution") {
		c.Attribution = defaultAttribution()
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
	if err := validateAttribution(c.Attribution); err != nil {
		return err
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

var httpTokenPattern = regexp.MustCompile("^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

func validateAttribution(a attributionConfig) error {
	if len(a.JSONHeaders) == 0 && len(a.Baggage) == 0 {
		return errors.New("attribution must configure at least one output")
	}
	seenHeaders := make(map[string]struct{}, len(a.JSONHeaders))
	for _, header := range a.JSONHeaders {
		lower := strings.ToLower(header.Name)
		if !httpTokenPattern.MatchString(header.Name) || !strings.HasPrefix(lower, "x-") ||
			lower == "x-api-key" || lower == "x-litellm-api-key" ||
			lower == "x-real-ip" || lower == "x-http-method-override" ||
			lower == "x-litellm-agent-id" || lower == "x-litellm-customer-id" ||
			strings.HasPrefix(lower, "x-forwarded-") || strings.HasPrefix(lower, "x-original-") ||
			len(header.Fields) == 0 {
			return errors.New("attribution has invalid JSON header")
		}
		if _, duplicate := seenHeaders[lower]; duplicate {
			return errors.New("attribution has duplicate JSON header")
		}
		seenHeaders[lower] = struct{}{}
		if err := validateAttributionFields(header.Fields, false); err != nil {
			return err
		}
	}
	return validateAttributionFields(a.Baggage, true)
}

func validateAttributionFields(fields []attributionField, isBaggage bool) error {
	seen := make(map[string]struct{}, len(fields))
	for _, field := range fields {
		if field.Key == "" {
			return errors.New("attribution output key must be non-empty")
		}
		if isBaggage {
			if _, err := baggage.NewMemberRaw(field.Key, "test"); err != nil {
				return errors.New("attribution baggage key is invalid")
			}
		}
		switch field.Source {
		case verifiedNamespace, verifiedServiceAccount, verifiedPodName, verifiedPodUID:
		default:
			return errors.New("attribution source is unsupported")
		}
		if _, duplicate := seen[field.Key]; duplicate {
			return errors.New("attribution has duplicate output key")
		}
		seen[field.Key] = struct{}{}
	}
	return nil
}
