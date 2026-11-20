package settings

import (
	"errors"
	"fmt"
	"math"
	"net/url"
	"os"
	"slices"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
	"github.com/agentydragon/ducktape/agentplane/configfile"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"gopkg.in/yaml.v3"
)

// Config is the app's complete deployment-authored settings file plus explicit runtime Secret inputs.
type Config struct {
	EgressAdminTLS                      *peertls.ClientTLS         `mapstructure:"egress_admin_tls"`
	ActionsTLS                          *peertls.ClientTLS         `mapstructure:"actions_tls"`
	NotificationsTLS                    *peertls.ClientTLS         `mapstructure:"notifications_tls"`
	HistoryServiceTLS                   *peertls.ClientTLS         `mapstructure:"history_service_tls"`
	SessionDispatcherTLS                *peertls.ClientTLS         `mapstructure:"session_dispatcher_tls"`
	EnvironmentService                  *EnvironmentServicePeer    `mapstructure:"environment_service"`
	RunnerGatewayTLS                    *peertls.ClientTLS         `mapstructure:"runner_gateway_tls"`
	PolicyServiceTLS                    *peertls.ClientTLS         `mapstructure:"policy_service_tls"`
	ConnectionServiceTLS                *peertls.ClientTLS         `mapstructure:"connection_service_tls"`
	Models                              runtimeconfig.ModelCatalog `mapstructure:"models"`
	ThreadPresets                       map[string]ThreadPreset    `mapstructure:"thread_presets"`
	SandboxPresets                      map[string]SandboxPreset   `mapstructure:"sandbox_presets"`
	NewSandboxDefaults                  NewSandboxDefaults         `mapstructure:"new_sandbox_defaults"`
	EgressAdminURL                      string                     `mapstructure:"egress_admin_url"`
	ConnectionServiceURL                string                     `mapstructure:"connection_service_url"`
	ActionFederation                    ActionFederationConfig     `mapstructure:"action_federation"`
	OIDCSettings                        *auth.OIDCSettings         `mapstructure:"oidc"`
	OIDC                                *auth.OIDCConfig           `mapstructure:"-"`
	FrontendConfig                      FrontendConfig             `mapstructure:"frontend_config"`
	SandboxNamespace                    string                     `mapstructure:"sandbox_namespace"`
	RunnerGatewayTarget                 string                     `mapstructure:"runner_gateway_target"`
	SessionDispatcherTarget             string                     `mapstructure:"session_dispatcher_target"`
	PolicyServiceTarget                 string                     `mapstructure:"policy_service_target"`
	PolicyServiceTokenFile              string                     `mapstructure:"policy_service_token_file"`
	HistoryServiceTarget                string                     `mapstructure:"history_service_target"`
	NotificationsURL                    string                     `mapstructure:"notifications_url"`
	NotificationsTokenFile              string                     `mapstructure:"notifications_token_file"`
	NotificationsRequestTimeoutSeconds  float64                    `mapstructure:"notifications_request_timeout_s"`
	RunnerGatewayTokenFile              string                     `mapstructure:"runner_gateway_token_file"`
	SessionDispatcherTokenFile          string                     `mapstructure:"session_dispatcher_token_file"`
	SessionDispatcherRequestTimeoutSecs float64                    `mapstructure:"session_dispatcher_request_timeout_s"`
	HistoryServiceTokenFile             string                     `mapstructure:"history_service_token_file"`
	HistoryServiceRequestTimeoutSeconds float64                    `mapstructure:"history_service_request_timeout_s"`
	RunnerGatewayRequestTimeoutSeconds  float64                    `mapstructure:"runner_gateway_request_timeout_s"`
	RunnerGatewayLifecycleTimeoutSecs   float64                    `mapstructure:"runner_gateway_lifecycle_timeout_s"`
	LogLevels                           map[string]string          `mapstructure:"log_levels"`
	ListenAddress                       string                     `mapstructure:"listen_address"`
	Kubeconfig                          string                     `mapstructure:"kubeconfig"`
	DatabaseURL                         string                     `mapstructure:"-"`
	ElectricURL                         string                     `mapstructure:"electric_url"`
	EgressAdminTokenFile                string                     `mapstructure:"egress_admin_token_file"`
	EgressAdminTimeoutSeconds           float64                    `mapstructure:"egress_admin_timeout"`
	ShutdownTimeoutSeconds              int                        `mapstructure:"shutdown_timeout"`
	sandboxPresetOrder                  []string
}

// EnvironmentServicePeer is an optional dedicated EnvironmentService route. Its SVID is required
// because this peer targets the receiver introduced by the receiver-before-config rollout.
type EnvironmentServicePeer struct {
	Target                string             `mapstructure:"target"`
	RequestTimeoutSeconds float64            `mapstructure:"request_timeout_s"`
	TLS                   *peertls.ClientTLS `mapstructure:"tls"`
}

// ServicePeer is a resolved backend connection. When no dedicated EnvironmentService peer is
// configured, the legacy Session Dispatcher endpoint and credentials remain in use.
type ServicePeer struct {
	Target                string
	TokenFile             string
	RequestTimeoutSeconds float64
	TLS                   *peertls.ClientTLS
}

func (c Config) EnvironmentServicePeer() ServicePeer {
	if peer := c.EnvironmentService; peer != nil {
		return ServicePeer{
			Target: peer.Target, RequestTimeoutSeconds: peer.RequestTimeoutSeconds, TLS: peer.TLS,
		}
	}
	return ServicePeer{
		Target: c.SessionDispatcherTarget, TokenFile: c.SessionDispatcherTokenFile,
		RequestTimeoutSeconds: c.SessionDispatcherRequestTimeoutSecs, TLS: c.SessionDispatcherTLS,
	}
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
		NotificationsRequestTimeoutSeconds:  5,
		SessionDispatcherRequestTimeoutSecs: 20,
		HistoryServiceRequestTimeoutSeconds: 20,
		RunnerGatewayRequestTimeoutSeconds:  20,
		RunnerGatewayLifecycleTimeoutSecs:   310,
		LogLevels:                           map[string]string{},
		ListenAddress:                       "127.0.0.1:8080",
		EgressAdminTimeoutSeconds:           5,
		ShutdownTimeoutSeconds:              5,
	}
}

// Load reads the required deployment settings and explicit runtime Secret inputs.
func Load() (Config, error) {
	c := defaultConfig()
	var sandboxPresetOrder []string
	path := os.Getenv("AGENTPLANE_CONFIG_FILE")
	if path == "" {
		return Config{}, errors.New("AGENTPLANE_CONFIG_FILE is required")
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("could not read app settings file: %w", err)
	}
	// Keep Viper's weak scalar conversions for compatibility with existing settings files.
	rawSettings, err := configfile.DecodeMapstructureYAML(contents, &c, configfile.MapstructureOptions{WeaklyTypedInput: true})
	if err != nil {
		switch {
		case errors.Is(err, configfile.ErrInvalidYAML):
			return Config{}, fmt.Errorf("could not decode app settings file: %w", err)
		case errors.Is(err, configfile.ErrMultipleDocuments):
			return Config{}, errors.New("app settings file must contain exactly one YAML document")
		default:
			return Config{}, fmt.Errorf("invalid app settings: %w", err)
		}
	}
	sandboxPresetOrder = yamlMappingKeys(contents, "sandbox_presets")
	c.DatabaseURL = os.Getenv("AGENTPLANE_DATABASE_URL")
	if c.LogLevels == nil {
		c.LogLevels = map[string]string{}
	}
	models, _ := rawSection(rawSettings, "models")
	if !rawHasKey(models, "models") {
		return Config{}, errors.New("models.models is required")
	}
	if !rawHasKey(models, "harnesses") {
		return Config{}, errors.New("models.harnesses is required")
	}
	c.Models.NormalizeHarnessNames()
	c.OIDC, err = auth.LoadOIDC(c.OIDCSettings)
	if err != nil {
		return Config{}, fmt.Errorf("invalid OIDC settings: %w", err)
	}
	c.applyDefaults(rawSettings)
	if err := c.validate(); err != nil {
		return Config{}, err
	}
	c.sandboxPresetOrder = sandboxPresetOrder
	return c, nil
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
		c.Models.Models = []runtimeconfig.ModelOption{}
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
		"policy_service_target":             c.PolicyServiceTarget,
		"history_service_target":            c.HistoryServiceTarget,
		"notifications_url":                 c.NotificationsURL,
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
	if c.PolicyServiceTLS == nil {
		return errors.New("policy_service_tls is required")
	}
	for _, caller := range []struct {
		name, tokenFile string
		serverURIs      []string
		tls             *peertls.ClientTLS
	}{
		{"session_dispatcher", c.SessionDispatcherTokenFile, []string{peertls.SessionDispatcherURI}, c.SessionDispatcherTLS},
		{"runner_gateway", c.RunnerGatewayTokenFile, []string{peertls.SessionDispatcherURI, peertls.SandboxControllerURI}, c.RunnerGatewayTLS},
		{"history_service", c.HistoryServiceTokenFile, []string{peertls.HistoryIngressURI}, c.HistoryServiceTLS},
		{"policy_service", c.PolicyServiceTokenFile, []string{peertls.PolicyControllerURI}, c.PolicyServiceTLS},
		{"egress_admin", c.EgressAdminTokenFile, []string{peertls.EgressProxyURI}, c.EgressAdminTLS},
		{"notifications", c.NotificationsTokenFile, []string{peertls.NotificationsURI}, c.NotificationsTLS},
	} {
		if err := validateAppServiceCaller(caller.name, caller.tls, caller.tokenFile, caller.serverURIs); err != nil {
			return err
		}
	}
	if peer := c.EnvironmentService; peer != nil {
		if strings.TrimSpace(peer.Target) == "" {
			return errors.New("environment_service.target is required")
		}
		if peer.RequestTimeoutSeconds <= 0 || math.IsNaN(peer.RequestTimeoutSeconds) || math.IsInf(peer.RequestTimeoutSeconds, 0) {
			return errors.New("environment_service.request_timeout_s must be positive")
		}
		if err := validateAppServiceCaller("environment_service", peer.TLS, "", []string{peertls.SandboxControllerURI}); err != nil {
			return err
		}
	}
	for _, peer := range []struct {
		name, serverURI string
		tls             *peertls.ClientTLS
	}{
		{"actions", peertls.ActionsURI, c.ActionsTLS},
		{"connection_service", peertls.MCPEdgeURI, c.ConnectionServiceTLS},
	} {
		if err := validateOptionalAppPeerTLS(peer.name, peer.tls, peer.serverURI); err != nil {
			return err
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

func validateAppServiceCaller(name string, tls *peertls.ClientTLS, tokenFile string, serverURIs []string) error {
	if tls != nil && !slices.Contains(serverURIs, tls.ServerURI) {
		return fmt.Errorf("%s_tls.server_uri must identify one of %q", name, serverURIs)
	}
	if err := tls.ValidateCaller(tokenFile, peertls.AppURI); err != nil {
		return fmt.Errorf("%s caller authentication: %w", name, err)
	}
	return nil
}

// validateOptionalAppPeerTLS covers facades whose per-request operator bearer remains the
// authority. An App SVID may add transport identity, without replacing that dynamic credential.
func validateOptionalAppPeerTLS(name string, tls *peertls.ClientTLS, serverURI string) error {
	if tls == nil {
		return nil
	}
	if err := tls.Validate(); err != nil {
		return fmt.Errorf("%s_tls: %w", name, err)
	}
	if tls.ServerURI != serverURI {
		return fmt.Errorf("%s_tls.server_uri must identify %s", name, serverURI)
	}
	if tls.UsesSVID() {
		if err := tls.ValidateCaller("", peertls.AppURI); err != nil {
			return fmt.Errorf("%s caller authentication: %w", name, err)
		}
	}
	return nil
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

func validateCatalog(catalog runtimeconfig.ModelCatalog, threadPresets map[string]ThreadPreset, sandboxPresets map[string]SandboxPreset) error {
	known := map[string]runtimeconfig.ModelOption{}
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
