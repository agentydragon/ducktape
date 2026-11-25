package server

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/configfile"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"go.opentelemetry.io/otel/baggage"
)

const (
	configFileEnv = "AGENTPLANE_LLM_INGRESS_CONFIG_FILE"
	liteLLMKeyEnv = "AGENTPLANE_LLM_INGRESS_LITELLM_KEY"
)

type verifiedClaim string

const (
	verifiedNamespace      verifiedClaim = "namespace"
	verifiedServiceAccount verifiedClaim = "service_account"
	verifiedPodName        verifiedClaim = "pod_name"
	verifiedPodUID         verifiedClaim = "pod_uid"
	// An Identity caller has these instead of namespace and service_account.
	verifiedIdentity    verifiedClaim = "identity"
	verifiedIdentityUID verifiedClaim = "identity_uid"
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
			{Key: "agentplane.identity", Source: verifiedIdentity},
			{Key: "agentplane.identity_uid", Source: verifiedIdentityUID},
		},
	}}}
}

type modelConfig struct {
	Model                    string `json:"model" mapstructure:"model"`
	TotalContextBudgetTokens int    `json:"total_context_budget_tokens" mapstructure:"total_context_budget_tokens"`
}

type config struct {
	ModelConfigServiceCallers []string `mapstructure:"model_config_service_callers"`
	CentralSamplerServices    []string `mapstructure:"central_sampler_services"`
	// CentralSamplerIdentityCallers enables the exact service actors to call for a live Identity.
	CentralSamplerIdentityCallers   bool     `mapstructure:"central_sampler_identity_callers"`
	AllowedServiceAccountNamespaces []string `mapstructure:"allowed_service_account_namespaces"`
	// PolicyNamespace holds the Identities a Sandbox SVID may name.
	PolicyNamespace       string            `mapstructure:"policy_namespace"`
	LogLLMRequests        bool              `mapstructure:"log_llm_requests"`
	Models                []modelConfig     `mapstructure:"models"`
	Attribution           attributionConfig `mapstructure:"attribution"`
	CodexModelCatalogFile string            `mapstructure:"codex_model_catalog_file"`
	LiteLLMURL            string            `mapstructure:"litellm_url"`
	LiteLLMKey            string            `mapstructure:"-"`
	LogLevels             map[string]string `mapstructure:"log_levels"`
	RPC                   listener.Config   `mapstructure:"rpc"`
	Kubeconfig            string            `mapstructure:"kubeconfig"`
}

func defaultConfig() config {
	return config{
		Attribution: defaultAttribution(), LogLevels: map[string]string{},
	}
}

func loadConfig() (config, error) {
	path, ok := os.LookupEnv(configFileEnv)
	if !ok || strings.TrimSpace(path) == "" {
		return config{}, fmt.Errorf("%s is required", configFileEnv)
	}
	c := defaultConfig()
	if _, err := configfile.DecodeMapstructureFile(path, &c, configfile.MapstructureOptions{
		DecodeHook:      listener.AuthenticationDecodeHook,
		ExactFieldNames: true,
	}); err != nil {
		return config{}, fmt.Errorf("decode LLM ingress settings file: %w", err)
	}
	c.LiteLLMKey = os.Getenv(liteLLMKeyEnv)
	if err := validateConfig(&c); err != nil {
		return config{}, err
	}
	return c, nil
}

func validateConfig(c *config) error {
	for _, callers := range [][]string{c.ModelConfigServiceCallers, c.CentralSamplerServices} {
		if err := auth.ValidateServiceCallers(callers); err != nil {
			return err
		}
	}
	if len(c.CentralSamplerServices) > 0 && !c.CentralSamplerIdentityCallers {
		return errors.New("central_sampler_services requires central_sampler_identity_callers")
	}
	if len(c.AllowedServiceAccountNamespaces) == 0 {
		return errors.New("allowed_service_account_namespaces must be non-empty")
	}
	if c.PolicyNamespace == "" {
		return errors.New("policy_namespace is required")
	}
	for _, namespace := range c.AllowedServiceAccountNamespaces {
		if namespace == "" {
			return errors.New("allowed_service_account_namespaces cannot contain an empty namespace")
		}
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
	if !attributesIdentity(c.Attribution) {
		return errors.New("policy_namespace requires every attribution output to carry the identity claim")
	}
	if c.CentralSamplerIdentityCallers && len(c.CentralSamplerServices) == 0 {
		return errors.New("central_sampler_identity_callers requires central_sampler_services")
	}
	if err := c.RPC.Validate(); err != nil {
		return err
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
		case verifiedNamespace, verifiedServiceAccount, verifiedPodName, verifiedPodUID, verifiedIdentity, verifiedIdentityUID:
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

// attributesIdentity reports whether every output names an Identity caller, which has no namespace
// or service_account claim.
func attributesIdentity(a attributionConfig) bool {
	carriesIdentity := func(fields []attributionField) bool {
		return slices.ContainsFunc(fields, func(field attributionField) bool { return field.Source == verifiedIdentity })
	}
	if len(a.Baggage) > 0 && !carriesIdentity(a.Baggage) {
		return false
	}
	return !slices.ContainsFunc(a.JSONHeaders, func(header attributionJSONHeader) bool { return !carriesIdentity(header.Fields) })
}
