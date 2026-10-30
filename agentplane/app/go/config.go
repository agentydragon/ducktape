package main

import (
	"errors"
	"flag"
	"fmt"
	"math"
	"net/url"
	"os"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/danielgtaylor/huma/v2"
	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
	"gopkg.in/yaml.v3"
)

// Config mirrors the deployed integration app's AGENTPLANE_* and YAML settings contract.
// OIDC relying-party settings are loaded separately by the auth package from AGENTPLANE_OIDC_*.
type Config struct {
	Models                              ModelCatalog             `mapstructure:"models"`
	ThreadPresets                       map[string]ThreadPreset  `mapstructure:"thread_presets"`
	SandboxPresets                      map[string]SandboxPreset `mapstructure:"sandbox_presets"`
	DefaultPolicySets                   []string                 `mapstructure:"default_policy_sets"`
	NewSandboxDefaults                  NewSandboxDefaults       `mapstructure:"new_sandbox_defaults"`
	EgressAdminURL                      string                   `mapstructure:"egress_admin_url"`
	ConnectionServiceURL                string                   `mapstructure:"connection_service_url"`
	ActionFederation                    ActionFederationConfig   `mapstructure:"action_federation"`
	FrontendConfig                      FrontendConfig           `mapstructure:"frontend_config"`
	SandboxNamespace                    string                   `mapstructure:"sandbox_namespace"`
	RunnerGatewayTarget                 string                   `mapstructure:"runner_gateway_target"`
	SessionDispatcherTarget             string                   `mapstructure:"session_dispatcher_target"`
	RunnerGatewayControlTarget          string                   `mapstructure:"runner_gateway_control_target"`
	HistoryServiceTarget                string                   `mapstructure:"history_service_target"`
	NotificationsURL                    string                   `mapstructure:"notifications_url"`
	NotificationsTokenFile              string                   `mapstructure:"notifications_token_file"`
	NotificationsRequestTimeoutSeconds  float64                  `mapstructure:"notifications_request_timeout_s"`
	RunnerGatewayTokenFile              string                   `mapstructure:"runner_gateway_token_file"`
	SessionDispatcherTokenFile          string                   `mapstructure:"session_dispatcher_token_file"`
	SessionDispatcherRequestTimeoutSecs float64                  `mapstructure:"session_dispatcher_request_timeout_s"`
	HistoryServiceTokenFile             string                   `mapstructure:"history_service_token_file"`
	HistoryServiceRequestTimeoutSeconds float64                  `mapstructure:"history_service_request_timeout_s"`
	RunnerGatewayRequestTimeoutSeconds  float64                  `mapstructure:"runner_gateway_request_timeout_s"`
	RunnerGatewayLifecycleTimeoutSecs   float64                  `mapstructure:"runner_gateway_lifecycle_timeout_s"`
	LogLevels                           map[string]string        `mapstructure:"log_levels"`
	ListenAddress                       string                   `mapstructure:"listen_address"`
	Kubeconfig                          string                   `mapstructure:"kubeconfig"`
	DatabaseURL                         string                   `mapstructure:"database_url"`
	ElectricURL                         string                   `mapstructure:"electric_url"`
	EgressAdminTokenFile                string                   `mapstructure:"egress_admin_token_file"`
	EgressAdminTimeoutSeconds           float64                  `mapstructure:"egress_admin_timeout"`
	ShutdownTimeoutSeconds              int                      `mapstructure:"shutdown_timeout"`
	ResyncSeconds                       int                      `mapstructure:"resync_seconds"`
	TokenAudience                       string                   `mapstructure:"token_audience"`
	TokenSubjects                       []string                 `mapstructure:"token_subjects"`
	sandboxPresetOrder                  []string
}

type ModelCatalog struct {
	Models         []ModelOption         `mapstructure:"models" json:"models"`
	Harnesses      map[string][]string   `mapstructure:"harnesses" json:"harnesses"`
	CentralSampler *CentralSamplerChoice `mapstructure:"central_sampler" json:"central_sampler"`
}

func (ModelCatalog) TransformSchema(_ huma.Registry, schema *huma.Schema) *huma.Schema {
	schema.Properties["central_sampler"] = &huma.Schema{AnyOf: []*huma.Schema{
		schema.Properties["central_sampler"], {Type: "null"},
	}}
	return schema
}

type CentralSamplerChoice struct {
	Service  string `mapstructure:"service" json:"service" minLength:"1"`
	Provider string `mapstructure:"provider" json:"provider" minLength:"1"`
}

type ModelOption struct {
	Model            string   `mapstructure:"model" json:"model"`
	DisplayName      string   `mapstructure:"display_name" json:"display_name"`
	ReasoningEfforts []string `mapstructure:"reasoning_efforts" json:"reasoning_efforts"`
}

type ThreadPreset struct {
	Title           string  `mapstructure:"title" json:"title"`
	Harness         string  `mapstructure:"harness" json:"harness"`
	Model           string  `mapstructure:"model" json:"model"`
	CWD             string  `mapstructure:"cwd" json:"cwd"`
	ReasoningEffort *string `mapstructure:"reasoning_effort" json:"reasoning_effort"`
	Instructions    string  `mapstructure:"instructions" json:"instructions"`
	SetupScript     string  `mapstructure:"setup_script" json:"setup_script"`
}

type SandboxPreset struct {
	Title        string   `mapstructure:"title" json:"title"`
	Template     string   `mapstructure:"template" json:"template"`
	PolicySets   []string `mapstructure:"policy_sets" json:"policy_sets"`
	InboxTags    []string `mapstructure:"inbox_tags" json:"inbox_tags"`
	ThreadPreset string   `mapstructure:"thread_preset" json:"thread_preset"`
	Bootstrap    string   `mapstructure:"bootstrap" json:"bootstrap"`
}

// NewSandboxDefaults are the operator-configured starting lifecycle policies for the create form.
type NewSandboxDefaults struct {
	WakeOnSend              bool   `mapstructure:"wake_on_send" json:"wake_on_send"`
	IdleSuspendAfterSeconds *int64 `mapstructure:"idle_suspend_after_seconds" json:"idle_suspend_after_seconds" nullable:"true" minimum:"60"`
}

type ActionFederationConfig struct {
	Mode              string             `mapstructure:"mode" json:"mode"`
	ServiceURL        string             `mapstructure:"service_url" json:"service_url"`
	LoginJWKsURI      string             `mapstructure:"login_jwks_uri" json:"login_jwks_uri"`
	LoginTokenProfile string             `mapstructure:"login_token_profile" json:"login_token_profile"`
	Target            OperatorOIDCConfig `mapstructure:"target" json:"target"`
	Scope             string             `mapstructure:"scope" json:"scope"`
	TokenEndpoint     *string            `mapstructure:"token_endpoint" json:"token_endpoint"`
}

type OperatorOIDCConfig struct {
	Issuer       string `mapstructure:"issuer" json:"issuer"`
	Audience     string `mapstructure:"audience" json:"audience"`
	JWKsURI      string `mapstructure:"jwks_uri" json:"jwks_uri"`
	TokenProfile string `mapstructure:"token_profile" json:"token_profile"`
}

type SessionDefaultsView struct {
	Harness         string  `json:"harness"`
	Model           string  `json:"model"`
	CWD             string  `json:"cwd"`
	ReasoningEffort *string `json:"reasoning_effort"`
	Instructions    string  `json:"instructions"`
	SetupScript     string  `json:"setup_script"`
}

type SandboxPresetView struct {
	Name            string              `json:"name"`
	Title           string              `json:"title"`
	Template        string              `json:"template"`
	PolicySets      []string            `json:"policy_sets" nullable:"false"`
	InboxTags       []string            `json:"inbox_tags" nullable:"false"`
	SessionDefaults SessionDefaultsView `json:"session_defaults"`
	Bootstrap       string              `json:"bootstrap"`
}

// PresetViews returns the browser-visible Sandbox presets with the ThreadPreset resolved into the
// same complete SessionDefaults object used when a thread is opened.
func (c Config) PresetViews() []SandboxPresetView {
	names := make([]string, 0, len(c.SandboxPresets))
	seen := make(map[string]struct{}, len(c.SandboxPresets))
	for _, name := range c.sandboxPresetOrder {
		if _, exists := c.SandboxPresets[name]; !exists {
			continue
		}
		if _, ok := seen[name]; ok {
			continue
		}
		seen[name] = struct{}{}
		names = append(names, name)
	}
	remaining := make([]string, 0, len(c.SandboxPresets)-len(names))
	for name := range c.SandboxPresets {
		if _, ok := seen[name]; !ok {
			remaining = append(remaining, name)
		}
	}
	sort.Strings(remaining)
	names = append(names, remaining...)
	views := make([]SandboxPresetView, 0, len(names))
	for _, name := range names {
		preset := c.SandboxPresets[name]
		thread := c.ThreadPresets[preset.ThreadPreset]
		views = append(views, SandboxPresetView{
			Name: name, Title: preset.Title, Template: preset.Template,
			PolicySets: nonNilStrings(preset.PolicySets), InboxTags: nonNilStrings(preset.InboxTags),
			SessionDefaults: SessionDefaultsView{
				Harness: thread.Harness, Model: thread.Model, CWD: thread.CWD,
				ReasoningEffort: thread.ReasoningEffort, Instructions: thread.Instructions, SetupScript: thread.SetupScript,
			},
			Bootstrap: preset.Bootstrap,
		})
	}
	return views
}

func nonNilStrings(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

func defaultConfig() Config {
	return Config{
		ThreadPresets:  map[string]ThreadPreset{},
		SandboxPresets: map[string]SandboxPreset{},
		ActionFederation: ActionFederationConfig{
			LoginTokenProfile: "authentik",
			Target:            OperatorOIDCConfig{TokenProfile: "authentik"},
		},
		NotificationsTokenFile:              "/var/run/secrets/agentplane-notifications/token",
		NotificationsRequestTimeoutSeconds:  5,
		RunnerGatewayTokenFile:              "/var/run/secrets/agentplane-runner-gateway/token",
		SessionDispatcherTokenFile:          "/var/run/secrets/agentplane-session-dispatcher/token",
		SessionDispatcherRequestTimeoutSecs: 20,
		HistoryServiceTokenFile:             "/var/run/secrets/agentplane-history-service/token",
		HistoryServiceRequestTimeoutSeconds: 20,
		RunnerGatewayRequestTimeoutSeconds:  20,
		RunnerGatewayLifecycleTimeoutSecs:   310,
		LogLevels:                           map[string]string{},
		ListenAddress:                       "127.0.0.1:8080",
		EgressAdminTokenFile:                "/var/run/secrets/agentplane-egress-admin/token",
		EgressAdminTimeoutSeconds:           5,
		ShutdownTimeoutSeconds:              5,
		ResyncSeconds:                       300,
		TokenAudience:                       "agentplane",
	}
}

func loadConfig() (Config, error) {
	return loadConfigWithArgs(nil)
}

func loadConfigWithArgs(args []string) (Config, error) {
	c := defaultConfig()
	var sandboxPresetOrder []string
	settings := viper.NewWithOptions(viper.ExperimentalBindStruct())
	settings.SetEnvPrefix("AGENTPLANE")
	settings.SetEnvKeyReplacer(strings.NewReplacer(".", "_"))
	settings.AutomaticEnv()
	var rawSettings map[string]any
	if path := os.Getenv("AGENTPLANE_CONFIG_FILE"); path != "" {
		settings.SetConfigFile(path)
		settings.SetConfigType("yaml")
		if err := settings.ReadInConfig(); err != nil {
			return Config{}, fmt.Errorf("could not read app settings file: %w", err)
		}
		contents, err := os.ReadFile(path)
		if err != nil {
			return Config{}, fmt.Errorf("could not read app settings file: %w", err)
		}
		if err := yaml.Unmarshal(contents, &rawSettings); err != nil {
			return Config{}, fmt.Errorf("could not decode app settings file: %w", err)
		}
		sandboxPresetOrder = yamlMappingKeys(contents, "sandbox_presets")
	}
	for _, key := range []string{"models", "thread_presets", "sandbox_presets", "new_sandbox_defaults", "frontend_config"} {
		if value, ok := os.LookupEnv("AGENTPLANE_" + strings.ToUpper(key)); ok {
			var decoded any
			if err := yaml.Unmarshal([]byte(value), &decoded); err != nil {
				return Config{}, fmt.Errorf("could not decode AGENTPLANE_%s: %w", strings.ToUpper(key), err)
			}
			if rawSettings == nil {
				rawSettings = map[string]any{}
			}
			rawSettings[key] = decoded
			if key == "frontend_config" {
				settings.Set(key, decoded)
			}
			if key == "sandbox_presets" {
				sandboxPresetOrder = yamlMappingKeys([]byte(value), "")
			}
		}
	}
	if err := settings.UnmarshalExact(&c, viper.DecodeHook(mapstructure.TextUnmarshallerHookFunc())); err != nil {
		return Config{}, fmt.Errorf("invalid app settings: %w", err)
	}
	for _, configured := range []struct {
		key    string
		target *[]string
	}{
		{key: "default_policy_sets", target: &c.DefaultPolicySets},
		{key: "token_subjects", target: &c.TokenSubjects},
	} {
		name := "AGENTPLANE_" + strings.ToUpper(configured.key)
		if value, ok := os.LookupEnv(name); ok {
			if err := yaml.Unmarshal([]byte(value), configured.target); err != nil {
				return Config{}, fmt.Errorf("could not decode %s: %w", name, err)
			}
		}
	}
	if c.LogLevels == nil {
		c.LogLevels = map[string]string{}
	}
	for _, key := range []string{"models.models", "models.harnesses", "default_policy_sets"} {
		if !settings.IsSet(key) {
			return Config{}, fmt.Errorf("%s is required", key)
		}
	}
	if err := c.decodeCaseSensitiveSettings(rawSettings); err != nil {
		return Config{}, err
	}
	cliSettings, cliSandboxPresetOrder, err := applyCommandLineOverrides(&c, args)
	if err != nil {
		return Config{}, err
	}
	if len(cliSettings) != 0 {
		if rawSettings == nil {
			rawSettings = map[string]any{}
		}
		for key, value := range cliSettings {
			rawSettings[key] = value
		}
		if err := c.decodeCaseSensitiveSettings(rawSettings); err != nil {
			return Config{}, err
		}
	}
	c.applyDefaults(rawSettings)
	if err := c.validate(); err != nil {
		return Config{}, err
	}
	if len(cliSandboxPresetOrder) != 0 {
		sandboxPresetOrder = cliSandboxPresetOrder
	}
	c.sandboxPresetOrder = sandboxPresetOrder
	return c, nil
}

// applyCommandLineOverrides accepts the kebab-case flag for every exported Config field. Values
// use YAML scalars or JSON/YAML objects, matching the Python settings CLI's structured values.
// Call it after YAML and environment decoding so CLI values have the highest precedence.
func applyCommandLineOverrides(c *Config, args []string) (map[string]any, []string, error) {
	flags := flag.NewFlagSet("agentplane-app", flag.ContinueOnError)
	type configFlag struct {
		name  string
		key   string
		index int
		value *string
	}
	configType := reflect.TypeOf(*c)
	configFlags := make([]configFlag, 0, configType.NumField())
	for index := 0; index < configType.NumField(); index++ {
		field := configType.Field(index)
		if field.PkgPath != "" { // Ignore internal bookkeeping fields.
			continue
		}
		key, ok := field.Tag.Lookup("mapstructure")
		if !ok || key == "" || key == "-" {
			continue
		}
		name := strings.ReplaceAll(key, "_", "-")
		value := new(string)
		flags.StringVar(value, name, "", "Override "+key+" from YAML or AGENTPLANE_* configuration.")
		configFlags = append(configFlags, configFlag{name: name, key: key, index: index, value: value})
	}
	if err := flags.Parse(args); err != nil {
		return nil, nil, fmt.Errorf("invalid app command line: %w", err)
	}
	if flags.NArg() != 0 {
		return nil, nil, fmt.Errorf("unexpected app command line arguments: %v", flags.Args())
	}
	set := make(map[string]struct{})
	flags.Visit(func(parsed *flag.Flag) { set[parsed.Name] = struct{}{} })
	configValue := reflect.ValueOf(c).Elem()
	cliSettings := make(map[string]any)
	var sandboxPresetOrder []string
	for _, configured := range configFlags {
		if _, present := set[configured.name]; !present {
			continue
		}
		var decoded any
		if err := yaml.Unmarshal([]byte(*configured.value), &decoded); err != nil {
			return nil, nil, fmt.Errorf("invalid --%s value: %w", configured.name, err)
		}
		cliSettings[configured.key] = decoded
		if configured.key == "sandbox_presets" {
			sandboxPresetOrder = yamlMappingKeys([]byte(*configured.value), "")
		}
		target := reflect.New(configType.Field(configured.index).Type)
		decoder, err := mapstructure.NewDecoder(&mapstructure.DecoderConfig{
			Result: target.Interface(), TagName: "mapstructure", WeaklyTypedInput: true,
			ErrorUnused: true,
			DecodeHook:  mapstructure.TextUnmarshallerHookFunc(),
		})
		if err != nil {
			return nil, nil, fmt.Errorf("configure --%s decoder: %w", configured.name, err)
		}
		if err := decoder.Decode(decoded); err != nil {
			return nil, nil, fmt.Errorf("invalid --%s value: %w", configured.name, err)
		}
		configValue.Field(configured.index).Set(target.Elem())
	}
	return cliSettings, sandboxPresetOrder, nil
}

func yamlMappingKeys(contents []byte, section string) []string {
	var document yaml.Node
	if err := yaml.Unmarshal(contents, &document); err != nil || len(document.Content) == 0 {
		return nil
	}
	root := document.Content[0]
	if section != "" {
		for index := 0; index+1 < len(root.Content); index += 2 {
			if root.Content[index].Value == section {
				root = root.Content[index+1]
				break
			}
		}
	}
	if root.Kind != yaml.MappingNode {
		return nil
	}
	keys := make([]string, 0, len(root.Content)/2)
	for index := 0; index+1 < len(root.Content); index += 2 {
		keys = append(keys, root.Content[index].Value)
	}
	return keys
}

func (c *Config) decodeCaseSensitiveSettings(raw map[string]any) error {
	if models, ok := rawSection(raw, "models"); ok {
		if err := decodeMapSection(models, &c.Models); err != nil {
			return fmt.Errorf("invalid models settings: %w", err)
		}
	}
	if presets, ok := rawSection(raw, "thread_presets"); ok {
		// Viper case-folds arbitrary map keys while decoding. Replace its normalized copy with the
		// original YAML map so preset names retain their exact spelling.
		c.ThreadPresets = make(map[string]ThreadPreset)
		if err := decodeMapSection(presets, &c.ThreadPresets); err != nil {
			return fmt.Errorf("invalid thread_presets settings: %w", err)
		}
	}
	if presets, ok := rawSection(raw, "sandbox_presets"); ok {
		c.SandboxPresets = make(map[string]SandboxPreset)
		if err := decodeMapSection(presets, &c.SandboxPresets); err != nil {
			return fmt.Errorf("invalid sandbox_presets settings: %w", err)
		}
	}
	if defaults, ok := rawSection(raw, "new_sandbox_defaults"); ok {
		if err := decodeMapSection(defaults, &c.NewSandboxDefaults); err != nil {
			return fmt.Errorf("invalid new_sandbox_defaults settings: %w", err)
		}
	}
	if frontend, ok := rawSection(raw, "frontend_config"); ok {
		c.FrontendConfig = FrontendConfig{}
		if err := decodeMapSection(frontend, &c.FrontendConfig); err != nil {
			return fmt.Errorf("invalid frontend_config settings: %w", err)
		}
	}
	normalized := make(map[string][]string, len(c.Models.Harnesses))
	for harness, models := range c.Models.Harnesses {
		normalized[strings.ToUpper(harness)] = models
	}
	c.Models.Harnesses = normalized
	return nil
}

func decodeMapSection(value any, result any) error {
	decoder, err := mapstructure.NewDecoder(&mapstructure.DecoderConfig{
		Result:           result,
		TagName:          "mapstructure",
		WeaklyTypedInput: true,
		ErrorUnused:      true,
	})
	if err != nil {
		return err
	}
	return decoder.Decode(value)
}

func rawSection(raw map[string]any, key string) (any, bool) {
	for name, value := range raw {
		if strings.EqualFold(name, key) {
			return value, true
		}
	}
	return nil, false
}

func rawMap(value any) map[string]any {
	result := map[string]any{}
	switch typed := value.(type) {
	case map[string]any:
		return typed
	case map[any]any:
		for key, value := range typed {
			if text, ok := key.(string); ok {
				result[text] = value
			}
		}
	}
	return result
}

func rawHasKey(value any, key string) bool {
	for name := range rawMap(value) {
		if strings.EqualFold(name, key) {
			return true
		}
	}
	return false
}

func (c *Config) applyDefaults(raw map[string]any) {
	if langfuse := c.FrontendConfig.LangfuseSearch; langfuse != nil {
		frontend, _ := rawSection(raw, "frontend_config")
		settings, _ := rawSection(rawMap(frontend), "langfuse_search")
		if !rawHasKey(settings, "session_metadata_key") {
			langfuse.SessionMetadataKey = "attributes.litellm.metadata.spend_logs_metadata.agentplane.session_id"
		}
	}
	if c.Models.Models == nil {
		c.Models.Models = []ModelOption{}
	}
	for i := range c.Models.Models {
		if c.Models.Models[i].ReasoningEfforts == nil {
			c.Models.Models[i].ReasoningEfforts = []string{}
		}
	}
	for harness, models := range c.Models.Harnesses {
		if models == nil {
			c.Models.Harnesses[harness] = []string{}
		}
	}
	if c.DefaultPolicySets == nil {
		c.DefaultPolicySets = []string{}
	}
	if c.TokenSubjects == nil {
		c.TokenSubjects = []string{}
	}
	threadPresets, _ := rawSection(raw, "thread_presets")
	for name, preset := range c.ThreadPresets {
		fields := rawMap(threadPresets)[name]
		if !rawHasKey(fields, "cwd") {
			preset.CWD = "/state/workspaces/{session_id}"
		}
		if !rawHasKey(fields, "instructions") {
			preset.Instructions = ""
		}
		if !rawHasKey(fields, "setup_script") {
			preset.SetupScript = ""
		}
		c.ThreadPresets[name] = preset
	}
	for name, preset := range c.SandboxPresets {
		if preset.PolicySets == nil {
			preset.PolicySets = []string{}
		}
		if preset.InboxTags == nil {
			preset.InboxTags = []string{}
		}
		c.SandboxPresets[name] = preset
	}
}

func (c Config) validate() error {
	for name, value := range map[string]string{
		"sandbox_namespace":                 c.SandboxNamespace,
		"runner_gateway_target":             c.RunnerGatewayTarget,
		"session_dispatcher_target":         c.SessionDispatcherTarget,
		"runner_gateway_control_target":     c.RunnerGatewayControlTarget,
		"history_service_target":            c.HistoryServiceTarget,
		"notifications_url":                 c.NotificationsURL,
		"notifications_token_file":          c.NotificationsTokenFile,
		"runner_gateway_token_file":         c.RunnerGatewayTokenFile,
		"session_dispatcher_token_file":     c.SessionDispatcherTokenFile,
		"history_service_token_file":        c.HistoryServiceTokenFile,
		"database_url":                      c.DatabaseURL,
		"egress_admin_url":                  c.EgressAdminURL,
		"connection_service_url":            c.ConnectionServiceURL,
		"action_federation.service_url":     c.ActionFederation.ServiceURL,
		"action_federation.login_jwks_uri":  c.ActionFederation.LoginJWKsURI,
		"action_federation.target.issuer":   c.ActionFederation.Target.Issuer,
		"action_federation.target.audience": c.ActionFederation.Target.Audience,
		"action_federation.target.jwks_uri": c.ActionFederation.Target.JWKsURI,
		"action_federation.scope":           c.ActionFederation.Scope,
	} {
		if strings.TrimSpace(value) == "" {
			return fmt.Errorf("%s is required", name)
		}
	}
	if strings.TrimSpace(c.ListenAddress) == "" {
		return errors.New("listen_address is required")
	}
	if seconds := c.NewSandboxDefaults.IdleSuspendAfterSeconds; seconds != nil && *seconds < 60 {
		return errors.New("new_sandbox_defaults.idle_suspend_after_seconds must be at least 60")
	}
	for name, value := range map[string]float64{
		"notifications_request_timeout_s":      c.NotificationsRequestTimeoutSeconds,
		"session_dispatcher_request_timeout_s": c.SessionDispatcherRequestTimeoutSecs,
		"history_service_request_timeout_s":    c.HistoryServiceRequestTimeoutSeconds,
		"runner_gateway_request_timeout_s":     c.RunnerGatewayRequestTimeoutSeconds,
		"runner_gateway_lifecycle_timeout_s":   c.RunnerGatewayLifecycleTimeoutSecs,
		"egress_admin_timeout":                 c.EgressAdminTimeoutSeconds,
	} {
		if value <= 0 || math.IsNaN(value) || math.IsInf(value, 0) {
			return fmt.Errorf("%s must be positive", name)
		}
	}
	for name, raw := range map[string]string{
		"notifications_url":                 c.NotificationsURL,
		"connection_service_url":            c.ConnectionServiceURL,
		"action_federation.service_url":     c.ActionFederation.ServiceURL,
		"action_federation.target.issuer":   c.ActionFederation.Target.Issuer,
		"action_federation.target.jwks_uri": c.ActionFederation.Target.JWKsURI,
		"egress_admin_url":                  c.EgressAdminURL,
	} {
		if err := validateHTTPURL(name, raw); err != nil {
			return err
		}
	}
	if c.ElectricURL != "" {
		if err := validateHTTPURL("electric_url", c.ElectricURL); err != nil {
			return err
		}
	}
	if err := c.FrontendConfig.validate(); err != nil {
		return err
	}
	if c.ActionFederation.Mode != "exchange" && c.ActionFederation.Mode != "direct" {
		return errors.New("action_federation.mode must be exchange or direct")
	}
	if c.ActionFederation.LoginTokenProfile != "authentik" && c.ActionFederation.LoginTokenProfile != "dex" {
		return errors.New("action_federation.login_token_profile must be authentik or dex")
	}
	if c.ActionFederation.Target.TokenProfile != "authentik" && c.ActionFederation.Target.TokenProfile != "dex" {
		return errors.New("action_federation.target.token_profile must be authentik or dex")
	}
	if c.ActionFederation.Mode == "exchange" && (c.ActionFederation.TokenEndpoint == nil || strings.TrimSpace(*c.ActionFederation.TokenEndpoint) == "") {
		return errors.New("action_federation.token_endpoint is required in exchange mode")
	}
	if c.ActionFederation.TokenEndpoint != nil {
		if err := validateHTTPSOrLoopbackURL("action_federation.token_endpoint", *c.ActionFederation.TokenEndpoint); err != nil {
			return err
		}
	}
	if c.ActionFederation.Mode == "direct" && c.ActionFederation.TokenEndpoint != nil {
		return errors.New("action_federation.token_endpoint must be null in direct mode")
	}
	return validateCatalog(c.Models, c.ThreadPresets, c.SandboxPresets)
}

func validateHTTPURL(name, raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" || parsed.User != nil || parsed.ForceQuery || parsed.RawQuery != "" || strings.Contains(raw, "#") {
		return fmt.Errorf("%s must be an absolute HTTP(S) URL without credentials, query, or fragment", name)
	}
	if port := parsed.Port(); port != "" {
		if _, err := strconv.ParseUint(port, 10, 16); err != nil {
			return fmt.Errorf("%s must use a port between 0 and 65535", name)
		}
	}
	return nil
}

func validateHTTPSOrLoopbackURL(name, raw string) error {
	if err := validateHTTPURL(name, raw); err != nil {
		return err
	}
	parsed, _ := url.Parse(raw)
	if parsed.Scheme == "https" {
		return nil
	}
	host := strings.Trim(parsed.Hostname(), "[]")
	if host == "localhost" || strings.HasSuffix(host, ".localhost") || strings.HasPrefix(host, "127.") || host == "::1" {
		return nil
	}
	return fmt.Errorf("%s must use HTTPS or a loopback HTTP host", name)
}

func validateCatalog(catalog ModelCatalog, threadPresets map[string]ThreadPreset, sandboxPresets map[string]SandboxPreset) error {
	known := map[string]ModelOption{}
	for _, option := range catalog.Models {
		if _, exists := known[option.Model]; exists {
			return fmt.Errorf("duplicate model id %q", option.Model)
		}
		if len(option.ReasoningEfforts) != len(uniqueStrings(option.ReasoningEfforts)) {
			return fmt.Errorf("model %q has duplicate reasoning efforts", option.Model)
		}
		known[option.Model] = option
	}
	_, hasClaude := catalog.Harnesses["HARNESS_CLAUDE"]
	_, hasCodex := catalog.Harnesses["HARNESS_CODEX"]
	if !hasClaude || !hasCodex {
		return errors.New("models.harnesses must configure both native harnesses: HARNESS_CLAUDE and HARNESS_CODEX")
	}
	if choice := catalog.CentralSampler; choice != nil && (choice.Service == "" || choice.Provider == "") {
		return errors.New("models.central_sampler requires non-empty service and provider")
	}
	if len(catalog.Harnesses["HARNESS_CENTRAL_SAMPLER"]) > 0 && catalog.CentralSampler == nil {
		return errors.New("central code-mode models require models.central_sampler")
	}
	hasOffer := false
	for harness, models := range catalog.Harnesses {
		if harness != "HARNESS_CLAUDE" && harness != "HARNESS_CODEX" && harness != "HARNESS_CENTRAL_SAMPLER" {
			return fmt.Errorf("unknown harness %q", harness)
		}
		if len(models) > 0 {
			hasOffer = true
		}
		for _, model := range models {
			if _, exists := known[model]; !exists {
				return fmt.Errorf("harness %s references unknown model %q", harness, model)
			}
		}
	}
	if !hasOffer {
		return errors.New("models.harnesses must offer at least one model")
	}
	for name, preset := range threadPresets {
		if _, exists := catalog.Harnesses[preset.Harness]; !exists {
			return fmt.Errorf("ThreadPreset %q has unknown harness %q", name, preset.Harness)
		}
		found := false
		for _, model := range catalog.Harnesses[preset.Harness] {
			found = found || model == preset.Model
		}
		if !found {
			return fmt.Errorf("ThreadPreset %q model %q is not offered by %s", name, preset.Model, preset.Harness)
		}
		if preset.ReasoningEffort != nil {
			model := known[preset.Model]
			supported := false
			for _, effort := range model.ReasoningEfforts {
				supported = supported || effort == *preset.ReasoningEffort
			}
			if !supported {
				return fmt.Errorf("ThreadPreset %q reasoning effort %q is not supported by %q", name, *preset.ReasoningEffort, preset.Model)
			}
		}
		if utf8.RuneCountInString(preset.SetupScript) > 65_536 {
			return fmt.Errorf("ThreadPreset %q setup_script exceeds 65536 characters", name)
		}
	}
	for name, preset := range sandboxPresets {
		if _, exists := threadPresets[preset.ThreadPreset]; !exists {
			return fmt.Errorf("SandboxPreset %q names unknown ThreadPreset %q", name, preset.ThreadPreset)
		}
		if utf8.RuneCountInString(preset.Bootstrap) > 65_536 {
			return fmt.Errorf("SandboxPreset %q bootstrap exceeds 65536 characters", name)
		}
		for _, tag := range preset.InboxTags {
			if n := utf8.RuneCountInString(tag); n < 1 || n > 63 {
				return fmt.Errorf("SandboxPreset %q inbox tag length must be between 1 and 63 characters", name)
			}
		}
	}
	return nil
}

func uniqueStrings(values []string) map[string]struct{} {
	unique := make(map[string]struct{}, len(values))
	for _, value := range values {
		unique[value] = struct{}{}
	}
	return unique
}
