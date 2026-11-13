package main

import (
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/go-viper/mapstructure/v2"
	"go.opentelemetry.io/otel/baggage"
	"gopkg.in/yaml.v3"
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
	// CentralSamplerAccounts may claim a central Session (`x-agentplane-session-id`) on their own
	// bearer calls.
	CentralSamplerAccounts []string `mapstructure:"central_sampler_accounts"`
	// CentralSamplerIdentityCallers lets those accounts also call as delegating actors for an
	// Identity in policy_namespace, which the call is then attributed to. It is a grant of its own:
	// claiming a Session never implies asserting an Identity.
	CentralSamplerIdentityCallers   bool     `mapstructure:"central_sampler_identity_callers"`
	AllowedServiceAccountNamespaces []string `mapstructure:"allowed_service_account_namespaces"`
	// PolicyNamespace holds the Identities a Sandbox SVID may name.
	PolicyNamespace       string            `mapstructure:"policy_namespace"`
	TokenAudience         string            `mapstructure:"token_audience"`
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
	file, err := os.Open(path)
	if err != nil {
		return config{}, fmt.Errorf("open LLM ingress settings file: %w", err)
	}
	defer file.Close()

	c := defaultConfig()
	yamlDecoder := yaml.NewDecoder(file)
	var settings map[string]any
	if err := yamlDecoder.Decode(&settings); err != nil {
		return config{}, fmt.Errorf("decode LLM ingress settings file: %w", err)
	}
	var extra any
	if err := yamlDecoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err != nil {
			return config{}, fmt.Errorf("decode LLM ingress settings file: %w", err)
		}
		return config{}, errors.New("LLM ingress settings file must contain one YAML document")
	}
	settingsDecoder, err := mapstructure.NewDecoder(&mapstructure.DecoderConfig{
		Result: &c, TagName: "mapstructure", ErrorUnused: true,
		MatchName: func(mapKey, fieldName string) bool { return mapKey == fieldName },
		DecodeHook: mapstructure.ComposeDecodeHookFunc(
			mapstructure.TextUnmarshallerHookFunc(), listener.AuthenticationDecodeHook,
		),
	})
	if err != nil {
		return config{}, fmt.Errorf("prepare LLM ingress settings decoder: %w", err)
	}
	if err := settingsDecoder.Decode(settings); err != nil {
		return config{}, fmt.Errorf("decode LLM ingress settings file: %w", err)
	}
	c.LiteLLMKey = os.Getenv(liteLLMKeyEnv)
	if err := validateConfig(&c); err != nil {
		return config{}, err
	}
	return c, nil
}

func validateConfig(c *config) error {
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
	if !attributesIdentity(c.Attribution) {
		return errors.New("policy_namespace requires every attribution output to carry the identity claim")
	}
	if c.CentralSamplerIdentityCallers && len(c.CentralSamplerAccounts) == 0 {
		return errors.New("central_sampler_identity_callers requires central_sampler_accounts")
	}
	if err := c.RPC.Validate(); err != nil {
		return err
	}
	if c.RPC.TLS == nil {
		return errors.New("rpc.tls is required for the shared HTTP/2 listener")
	}
	if c.RPC.Has(listener.KubernetesBearer) && !c.RPC.AllowsBearerAudience(c.TokenAudience) {
		return errors.New("rpc bearer audiences must include token_audience")
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
