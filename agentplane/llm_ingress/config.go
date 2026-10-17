package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_LLM_INGRESS_CONFIG_FILE"

type modelConfig struct {
	Model                    string `json:"model" yaml:"model"`
	TotalContextBudgetTokens int    `json:"total_context_budget_tokens" yaml:"total_context_budget_tokens"`
}

type config struct {
	AllowedServiceAccountNamespaces []string          `json:"allowed_service_account_namespaces" yaml:"allowed_service_account_namespaces"`
	TokenAudience                   string            `json:"token_audience" yaml:"token_audience"`
	LogLLMRequests                  bool              `json:"log_llm_requests" yaml:"log_llm_requests"`
	Models                          []modelConfig     `json:"models" yaml:"models"`
	LiteLLMURL                      string            `json:"litellm_url" yaml:"litellm_url"`
	LiteLLMKey                      string            `json:"litellm_key" yaml:"litellm_key"`
	LogLevels                       map[string]string `json:"log_levels" yaml:"log_levels"`
	Host                            string            `json:"host" yaml:"host"`
	Port                            int               `json:"port" yaml:"port"`
	Kubeconfig                      string            `json:"kubeconfig" yaml:"kubeconfig"`
}

func defaultConfig() config {
	return config{Host: "0.0.0.0", Port: 8080, LogLevels: map[string]string{}}
}

func loadConfig(args, environ []string) (config, error) {
	c := defaultConfig()
	environment := make(map[string]string, len(environ))
	for _, item := range environ {
		name, value, found := strings.Cut(item, "=")
		if found {
			environment[name] = value
		}
	}
	if path := environment[configFileEnv]; path != "" {
		contents, err := os.ReadFile(path)
		if err != nil {
			return config{}, fmt.Errorf("could not read settings file %q", path)
		}
		if err := validateYAMLShapes(contents); err != nil {
			return config{}, errors.New("invalid settings YAML")
		}
		decoder := yaml.NewDecoder(bytes.NewReader(contents))
		decoder.KnownFields(true)
		if err := decoder.Decode(&c); err != nil {
			return config{}, errors.New("invalid settings YAML")
		}
		var trailing any
		if err := decoder.Decode(&trailing); err != io.EOF {
			return config{}, errors.New("settings YAML must contain one document")
		}
	}
	if err := applyEnvironment(&c, environment); err != nil {
		return config{}, err
	}
	if err := applyFlags(&c, args); err != nil {
		return config{}, err
	}
	if err := validateConfig(&c); err != nil {
		return config{}, err
	}
	return c, nil
}

func applyEnvironment(c *config, environment map[string]string) error {
	fields := map[string]func(string) error{
		"ALLOWED_SERVICE_ACCOUNT_NAMESPACES": func(value string) error {
			if err := decodeContainerJSON(value, '[', &c.AllowedServiceAccountNamespaces); err != nil {
				return errors.New("invalid AGENTPLANE_LLM_INGRESS_ALLOWED_SERVICE_ACCOUNT_NAMESPACES")
			}
			return nil
		},
		"TOKEN_AUDIENCE": func(value string) error { c.TokenAudience = value; return nil },
		"LOG_LLM_REQUESTS": func(value string) error {
			parsed, err := strconv.ParseBool(value)
			if err != nil {
				return errors.New("invalid AGENTPLANE_LLM_INGRESS_LOG_LLM_REQUESTS")
			}
			c.LogLLMRequests = parsed
			return nil
		},
		"MODELS": func(value string) error {
			if err := decodeContainerJSON(value, '[', &c.Models); err != nil {
				return errors.New("invalid AGENTPLANE_LLM_INGRESS_MODELS")
			}
			return nil
		},
		"LITELLM_URL": func(value string) error { c.LiteLLMURL = value; return nil },
		"LITELLM_KEY": func(value string) error { c.LiteLLMKey = value; return nil },
		"LOG_LEVELS": func(value string) error {
			if err := decodeContainerJSON(value, '{', &c.LogLevels); err != nil {
				return errors.New("invalid AGENTPLANE_LLM_INGRESS_LOG_LEVELS")
			}
			return nil
		},
		"HOST":       func(value string) error { c.Host = value; return nil },
		"PORT":       func(value string) error { return parseInt(value, "AGENTPLANE_LLM_INGRESS_PORT", &c.Port) },
		"KUBECONFIG": func(value string) error { c.Kubeconfig = value; return nil },
	}
	for name := range environment {
		if strings.HasPrefix(name, "AGENTPLANE_LLM_INGRESS_") && name != configFileEnv {
			suffix := strings.TrimPrefix(name, "AGENTPLANE_LLM_INGRESS_")
			if _, ok := fields[suffix]; !ok {
				return fmt.Errorf("unknown setting %s", name)
			}
		}
	}
	for suffix, apply := range fields {
		if value, ok := environment["AGENTPLANE_LLM_INGRESS_"+suffix]; ok {
			if err := apply(value); err != nil {
				return err
			}
		}
	}
	return nil
}

func applyFlags(c *config, args []string) error {
	flags := flag.NewFlagSet("agentplane-llm-ingress", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	var namespaces, models, logLevels, logLLMRequests string
	flags.StringVar(&namespaces, "allowed-service-account-namespaces", "", "accepted workload namespaces as a JSON list")
	flags.StringVar(&c.TokenAudience, "token-audience", c.TokenAudience, "accepted projected-token audience")
	flags.StringVar(&logLLMRequests, "log-llm-requests", strconv.FormatBool(c.LogLLMRequests), "log full LLM request and response bodies")
	flags.StringVar(&models, "models", "", "model configuration as a JSON list")
	flags.StringVar(&c.LiteLLMURL, "litellm-url", c.LiteLLMURL, "internal LiteLLM base URL")
	flags.StringVar(&c.LiteLLMKey, "litellm-key", c.LiteLLMKey, "server-held LiteLLM virtual key")
	flags.StringVar(&logLevels, "log-levels", "", "logger levels as a JSON object")
	flags.StringVar(&c.Host, "host", c.Host, "listener bind address")
	flags.IntVar(&c.Port, "port", c.Port, "listener port")
	flags.StringVar(&c.Kubeconfig, "kubeconfig", c.Kubeconfig, "Kubernetes client configuration; defaults to in-cluster credentials")
	if err := flags.Parse(args); err != nil {
		return errors.New("invalid command-line flags")
	}
	if flags.NArg() != 0 {
		return errors.New("unexpected positional arguments")
	}
	if value, err := strconv.ParseBool(logLLMRequests); err != nil {
		return errors.New("invalid --log-llm-requests")
	} else {
		c.LogLLMRequests = value
	}
	set := make(map[string]bool)
	flags.Visit(func(item *flag.Flag) { set[item.Name] = true })
	if set["allowed-service-account-namespaces"] {
		if err := decodeContainerJSON(namespaces, '[', &c.AllowedServiceAccountNamespaces); err != nil {
			return errors.New("invalid --allowed-service-account-namespaces")
		}
	}
	if set["models"] {
		if err := decodeContainerJSON(models, '[', &c.Models); err != nil {
			return errors.New("invalid --models")
		}
	}
	if set["log-levels"] {
		if err := decodeContainerJSON(logLevels, '{', &c.LogLevels); err != nil {
			return errors.New("invalid --log-levels")
		}
	}
	return nil
}

func decodeContainerJSON(value string, first byte, target any) error {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" || trimmed[0] != first {
		return errors.New("JSON value has the wrong shape")
	}
	return decodeStrictJSON(value, target)
}

func decodeStrictJSON(value string, target any) error {
	decoder := json.NewDecoder(strings.NewReader(value))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return errors.New("unexpected trailing JSON")
	}
	return nil
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

func parseInt(value, name string, target *int) error {
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fmt.Errorf("invalid %s", name)
	}
	*target = parsed
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
