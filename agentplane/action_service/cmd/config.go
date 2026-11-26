package main

import (
	"errors"
	"fmt"
	"math"
	"os"
	"regexp"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/push"
	"github.com/agentydragon/ducktape/agentplane/configfile"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/identity/operator"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	"gopkg.in/yaml.v3"
)

const (
	settingPrefix        = "AGENTPLANE_ACTIONS_"
	databaseURLSecretEnv = settingPrefix + "DATABASE_URL"
	webPushPrivateKeyEnv = settingPrefix + "WEB_PUSH__PRIVATE_KEY_PEM"
)

// config holds the required YAML settings plus the explicitly named runtime Secret inputs.
// DatabaseURL and the Web Push private key are never read from the settings file.
type config struct {
	ServiceReaders              []string        `mapstructure:"service_readers"`
	OperatorServices            []string        `mapstructure:"operator_services"`
	ConnectionDelegationService string          `mapstructure:"connection_delegation_service"`
	SessionDelegationService    string          `mapstructure:"session_delegation_service"`
	DatabaseURL                 string          `mapstructure:"-"`
	RPC                         listener.Config `mapstructure:"rpc"`
	// CallerServiceAccountNamespaces scopes live WorkloadCredential reads.
	CallerServiceAccountNamespaces []string `mapstructure:"caller_service_account_namespaces"`
	// OperatorOIDC is on when any of its settings differs from its default; off, no operator is admitted.
	OperatorOIDC operator.Settings `mapstructure:"operator_oidc"`
	// PolicyNamespace holds the PolicySets and PolicyBindings the service watches; it writes ActionsReady only on
	// those bindings.
	PolicyNamespace string `mapstructure:"policy_namespace"`
	// PolicyService, when set, is the one source of every Identity this service reads; unset, it reads them from the
	// API server in PolicyNamespace.
	// CLEANUP(added 2026-11-26): require it and drop the Kubernetes Identity reads once the staging manifests set it
	//   (agentplane/plans/task_dag.md IDENTITY_READERS_BEHIND_POLICY_SERVICE).
	PolicyService *identitycache.Peer `mapstructure:"policy_service"`
	// WebPush is on when any of its settings is set; off, no push is sent and no subscription is accepted.
	WebPush push.Settings `mapstructure:"web_push"`
	// ActionGroups is read from the file alone; see settingsFile.
	ActionGroups *catalog.Catalog `mapstructure:"-"`
	// MCPServers are the MCP servers an operator links an account to, by server ID.
	MCPServers map[string]linkage.Server `mapstructure:"mcp_servers"`
	// MCPClientMetadata is the client identity of the servers that use the shared Client ID Metadata Document.
	MCPClientMetadata linkage.ClientMetadata `mapstructure:"mcp_client_metadata"`
	// DirectWaitSeconds bounds orchestration_v1 Session operations.
	DirectWaitSeconds float64 `mapstructure:"direct_wait_seconds"`
	// SessionDispatcher is where the orchestration_v1 groups send Session commands, and HistoryService where they read
	// Sessions; both are set exactly when one is configured.
	SessionDispatcher *peer `mapstructure:"session_dispatcher"`
	HistoryService    *peer `mapstructure:"history_service"`
	// SessionDelegationDispatcher is the shared Discovery and Session Dispatcher listener called with forwarded
	// caller identity; SessionDelegationHistory is the History Service listener with its own audience token.
	SessionDelegationDispatcher *peer `mapstructure:"session_delegation_dispatcher"`
	SessionDelegationHistory    *peer `mapstructure:"session_delegation_history"`
	// EnvironmentService optionally overrides the EnvironmentService target while Dispatcher methods keep their
	// direct or delegated peer. It must use the Action Service SVID against the Controller identity.
	EnvironmentService *peer `mapstructure:"environment_service"`
}

// peer is a gRPC service this one calls as its workload, with its projected token for the peer's audience.
type peer struct {
	Target                string             `mapstructure:"target"`
	TokenFile             string             `mapstructure:"token_file"`
	RequestTimeoutSeconds float64            `mapstructure:"request_timeout_s"`
	TLS                   *peertls.ClientTLS `mapstructure:"tls"`
}

func defaultConfig() config {
	return config{
		OperatorOIDC: operator.DefaultSettings(), ActionGroups: &catalog.Catalog{}, DirectWaitSeconds: 30,
	}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	path := os.Getenv(settingPrefix + "CONFIG_FILE")
	if path == "" {
		return config{}, errors.New("AGENTPLANE_ACTIONS_CONFIG_FILE is required")
	}
	file, err := readSettingsFile(path)
	if err != nil {
		return config{}, err
	}
	if c.ActionGroups, err = catalog.New(file.ActionGroups); err != nil {
		return config{}, fmt.Errorf("action_groups: %w", err)
	}
	// The errors name no value: a setting can be a Secret.
	// Keep Viper's weak scalar conversions for compatibility with existing settings files.
	if err := configfile.DecodeMapstructure(file.Others, &c, configfile.MapstructureOptions{
		WeaklyTypedInput: true,
	}); err != nil {
		return config{}, errors.New("invalid Action Service settings")
	}
	c.DatabaseURL = os.Getenv(databaseURLSecretEnv)
	c.WebPush.PrivateKeyPEM = os.Getenv(webPushPrivateKeyEnv)
	for key, server := range c.MCPServers {
		if server.ClientIDFile == nil {
			continue
		}
		if server.ClientID != nil || server.UseSharedCIMD {
			return config{}, fmt.Errorf("mcp_servers.%s: client_id_file is exclusive with client_id and use_shared_cimd", key)
		}
		contents, err := os.ReadFile(*server.ClientIDFile)
		if err != nil {
			return config{}, fmt.Errorf("mcp_servers.%s: could not read client_id_file", key)
		}
		server.ClientID = new(strings.TrimSpace(string(contents)))
		server.ClientIDFile = nil
		c.MCPServers[key] = server
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

// operatorsEnabled is whether any operator OIDC setting differs from its default.
func (c *config) operatorsEnabled() bool { return c.OperatorOIDC != operator.DefaultSettings() }

func (c *config) validate() error {
	if err := c.RPC.Validate(); err != nil {
		return fmt.Errorf("rpc: %w", err)
	}
	for _, required := range []struct{ name, value string }{
		{"database_url", c.DatabaseURL},
		{"policy_namespace", c.PolicyNamespace},
	} {
		if required.value == "" {
			return errors.New(required.name + " is required")
		}
	}

	for _, services := range [][]string{c.ServiceReaders, c.OperatorServices} {
		if err := auth.ValidateServiceCallers(services); err != nil {
			return err
		}
	}
	if c.ConnectionDelegationService != "" {
		if err := auth.ValidateServiceCallers([]string{c.ConnectionDelegationService}); err != nil {
			return err
		}
	}
	if c.SessionDelegationService != "" {
		if c.SessionDelegationService != peertls.CentralSamplerURI {
			return fmt.Errorf("session_delegation_service must be %s", peertls.CentralSamplerURI)
		}
		if err := auth.ValidateServiceCallers([]string{c.SessionDelegationService}); err != nil {
			return err
		}
	}
	if c.PolicyService != nil {
		if err := c.PolicyService.Validate(peertls.ActionsURI); err != nil {
			return err
		}
	}
	if len(c.CallerServiceAccountNamespaces) == 0 {
		return errors.New("caller_service_account_namespaces must list at least one namespace")
	}

	if c.WebPush.Configured() {
		if err := c.WebPush.Validate(); err != nil {
			return err
		}
	}
	if err := linkage.Validate(c.MCPServers, c.MCPClientMetadata); err != nil {
		return err
	}
	if c.DirectWaitSeconds < 0 || math.IsNaN(c.DirectWaitSeconds) || math.IsInf(c.DirectWaitSeconds, 0) {
		return errors.New("direct_wait_seconds must be finite and nonnegative")
	}
	if err := c.validateOrchestration(); err != nil {
		return err
	}
	if err := c.validateSessionDelegation(); err != nil {
		return err
	}
	if err := c.validateEnvironmentService(); err != nil {
		return err
	}
	if c.operatorsEnabled() {
		return c.OperatorOIDC.Validate()
	}
	return nil
}

func (c *config) validateEnvironmentService() error {
	p := c.EnvironmentService
	if p == nil {
		return nil
	}
	if p.Target == "" || !(p.RequestTimeoutSeconds > 0) || math.IsInf(p.RequestTimeoutSeconds, 0) {
		return errors.New("environment_service requires target and a finite positive request_timeout_s")
	}
	if p.TLS == nil || p.TLS.ServerURI != peertls.SandboxControllerURI {
		return fmt.Errorf("environment_service.tls.server_uri must identify %s", peertls.SandboxControllerURI)
	}
	if !p.TLS.UsesSVID() {
		return errors.New("environment_service must present the Actions service SVID")
	}
	if err := p.TLS.ValidateCaller(p.TokenFile, peertls.ActionsURI); err != nil {
		return fmt.Errorf("environment_service authentication: %w", err)
	}
	return nil
}

// validateSessionDelegation requires the delegated Discovery/Dispatcher and History peers exactly when a
// session_delegation group calls them.
func (c *config) validateSessionDelegation() error {
	var delegated bool
	for _, group := range c.ActionGroups.Groups() {
		if _, ok := group.Executor.(catalog.SessionDelegationBinding); ok {
			delegated = true
		}
	}
	for _, called := range []struct {
		key  string
		peer *peer
	}{{"session_delegation_dispatcher", c.SessionDelegationDispatcher}, {"session_delegation_history", c.SessionDelegationHistory}} {
		switch p := called.peer; {
		case delegated && p == nil:
			return fmt.Errorf("%s is required by session_delegation groups", called.key)
		case !delegated && p != nil:
			return fmt.Errorf("%s is set, but no session_delegation group calls it", called.key)
		case p != nil:
			if p.Target == "" || !(p.RequestTimeoutSeconds > 0) || math.IsInf(p.RequestTimeoutSeconds, 0) {
				return fmt.Errorf("%s requires target and a finite positive request_timeout_s", called.key)
			}
			if err := p.TLS.ValidateCaller(p.TokenFile, peertls.ActionsURI); err != nil {
				return fmt.Errorf("%s authentication: %w", called.key, err)
			}
		}
	}
	return nil
}

// validateOrchestration requires the Session Dispatcher and the History Service exactly when an orchestration_v1 group
// calls them, and each such group to launch where the policy index reads its launcher grants.
func (c *config) validateOrchestration() error {
	var orchestration bool
	for key, group := range c.ActionGroups.Groups() {
		if binding, is := group.Executor.(catalog.OrchestrationBinding); is {
			orchestration = true
			if binding.Namespace != c.PolicyNamespace {
				return fmt.Errorf("ActionGroup %s must launch in policy_namespace, where its launcher grants are read", key)
			}
		}
	}
	for _, called := range []struct {
		key  string
		peer *peer
	}{{"session_dispatcher", c.SessionDispatcher}, {"history_service", c.HistoryService}} {
		switch p := called.peer; {
		case orchestration && p == nil:
			return fmt.Errorf("%s is required by the orchestration_v1 groups", called.key)
		case !orchestration && p != nil:
			return fmt.Errorf("%s is set, but no orchestration_v1 group calls it", called.key)
		case p != nil:
			if p.Target == "" || !(p.RequestTimeoutSeconds > 0) || math.IsInf(p.RequestTimeoutSeconds, 0) {
				return fmt.Errorf("%s requires target and a finite positive request_timeout_s", called.key)
			}
			if err := p.TLS.ValidateCaller(p.TokenFile, peertls.ActionsURI); err != nil {
				return fmt.Errorf("%s authentication: %w", called.key, err)
			}
		}
	}
	return nil
}

// settingsFile is the settings file as gopkg.in/yaml.v3 decodes it.
type settingsFile struct {
	// ActionGroups has its own strict decoder; input schema and MCP server keys retain their spelling.
	ActionGroups catalog.Settings `yaml:"action_groups"`
	// Others are decoded with mapstructure so unknown service settings fail startup.
	Others map[string]any `yaml:",inline"`
}

func readSettingsFile(path string) (settingsFile, error) {
	var file settingsFile
	if err := configfile.DecodeKnownFieldsFile(path, &file); err != nil {
		var readError *os.PathError
		switch {
		case errors.As(err, &readError):
			return file, errors.New("could not read Action Service settings file")
		case errors.Is(err, configfile.ErrMultipleDocuments), errors.Is(err, configfile.ErrTrailingYAML):
			return file, errors.New("Action Service settings file must contain exactly one YAML document")
		default:
			return file, fmt.Errorf("settings file: %w", withoutValues(err))
		}
	}
	return file, nil
}

// quotedValue is the value a yaml.v3 type error quotes, from its opening backtick to the error's last.
var quotedValue = regexp.MustCompile("(?s) `.*`")

// withoutValues is err with the values yaml.v3 quotes cut out: a setting can be a credential.
func withoutValues(err error) error {
	var typeError *yaml.TypeError
	if !errors.As(err, &typeError) {
		return err
	}
	problems := make([]string, len(typeError.Errors))
	for i, problem := range typeError.Errors {
		problems[i] = quotedValue.ReplaceAllString(problem, "")
	}
	return errors.New(strings.Join(problems, "; "))
}
