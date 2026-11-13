package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"regexp"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/push"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
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
	DatabaseURL string          `mapstructure:"-"`
	RPC         listener.Config `mapstructure:"rpc"`
	// CallerServiceAccountNamespaces hold the ServiceAccounts whose workloads may call; a labeled account outside
	// them is refused.
	CallerServiceAccountNamespaces []string `mapstructure:"caller_service_account_namespaces"`
	// ConnectionDelegationActor is the MCP edge's ServiceAccount, which may delegate a live Identity on the
	// explicitly configured Action methods while preserving its own Action-audience token as the actor.
	ConnectionDelegationActor *auth.Account `mapstructure:"connection_delegation_actor"`
	// ReaderAccounts read every account's requests and change none, written `namespace/name` or as a mapping.
	ReaderAccounts []auth.Account `mapstructure:"reader_accounts"`
	// OperatorOIDC is on when any of its settings differs from its default; off, no operator is admitted.
	OperatorOIDC api.OperatorOIDCSettings `mapstructure:"operator_oidc"`
	// PolicyNamespace holds the PolicySets and PolicyBindings the service watches; it writes ActionsReady only on
	// those bindings.
	PolicyNamespace string `mapstructure:"policy_namespace"`
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
}

// peer is a gRPC service this one calls as its workload, with its projected token for the peer's audience.
type peer struct {
	Target                string                                   `mapstructure:"target"`
	TokenFile             string                                   `mapstructure:"token_file"`
	RequestTimeoutSeconds float64                                  `mapstructure:"request_timeout_s"`
	TLS                   *environment_runtime_transport.ClientTLS `mapstructure:"tls"`
}

func defaultConfig() config {
	return config{
		OperatorOIDC: api.DefaultOperatorOIDCSettings(), ActionGroups: &catalog.Catalog{}, DirectWaitSeconds: 30,
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
	settings := viper.New()
	if err := settings.MergeConfigMap(file.Others); err != nil {
		return config{}, errors.New("could not read Action Service settings file")
	}
	// The errors name no value: a setting can be a Secret.
	decodeHook := mapstructure.ComposeDecodeHookFunc(
		mapstructure.TextUnmarshallerHookFunc(),
		mapstructure.DecodeHookFuncType(listener.AuthenticationDecodeHook),
	)
	if err := settings.UnmarshalExact(&c, viper.DecodeHook(decodeHook)); err != nil {
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
func (c *config) operatorsEnabled() bool { return c.OperatorOIDC != api.DefaultOperatorOIDCSettings() }

func (c *config) validate() error {
	if err := c.RPC.Validate(); err != nil {
		return fmt.Errorf("rpc: %w", err)
	}
	if !c.RPC.Has(listener.KubernetesBearer) {
		return errors.New("rpc.auth must include kubernetes_bearer")
	}
	if len(c.rpcBearerAudiences()) != 1 {
		return errors.New("rpc kubernetes_bearer must configure exactly one audience")
	}
	for _, required := range []struct{ name, value string }{
		{"database_url", c.DatabaseURL},
		{"policy_namespace", c.PolicyNamespace},
	} {
		if required.value == "" {
			return errors.New(required.name + " is required")
		}
	}
	for _, account := range c.ReaderAccounts {
		if err := new(auth.Account).UnmarshalText([]byte(account.Namespace + "/" + account.Name)); err != nil {
			return errors.New("reader_accounts requires namespace and name")
		}
	}
	if len(c.CallerServiceAccountNamespaces) == 0 {
		return errors.New("caller_service_account_namespaces must list at least one namespace")
	}
	if c.ConnectionDelegationActor != nil {
		if err := new(auth.Account).UnmarshalText([]byte(c.ConnectionDelegationActor.Namespace + "/" + c.ConnectionDelegationActor.Name)); err != nil {
			return errors.New("connection_delegation_actor requires namespace and name")
		}
		allowed := false
		for _, namespace := range c.CallerServiceAccountNamespaces {
			allowed = allowed || namespace == c.ConnectionDelegationActor.Namespace
		}
		if !allowed {
			return errors.New("connection_delegation_actor namespace must be in caller_service_account_namespaces")
		}
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
	if c.operatorsEnabled() {
		return c.OperatorOIDC.Validate()
	}
	return nil
}

func (c config) rpcBearerAudiences() []string {
	for _, method := range c.RPC.Auth {
		if bearer, ok := method.(*listener.KubernetesBearerAuth); ok && bearer != nil {
			return bearer.Audiences
		}
	}
	return nil
}

func (c config) tokenAudience() string { return c.rpcBearerAudiences()[0] }

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
			if p.Target == "" || p.TokenFile == "" || !(p.RequestTimeoutSeconds > 0) || math.IsInf(p.RequestTimeoutSeconds, 0) {
				return fmt.Errorf("%s requires target, token_file and a finite positive request_timeout_s", called.key)
			}
			if p.TLS != nil {
				if err := p.TLS.Validate(); err != nil {
					return fmt.Errorf("%s tls: %w", called.key, err)
				}
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
			if p.Target == "" || p.TokenFile == "" || !(p.RequestTimeoutSeconds > 0) || math.IsInf(p.RequestTimeoutSeconds, 0) {
				return fmt.Errorf("%s requires target, token_file and a finite positive request_timeout_s", called.key)
			}
			if p.TLS != nil {
				if err := p.TLS.Validate(); err != nil {
					return fmt.Errorf("%s tls: %w", called.key, err)
				}
			}
		}
	}
	return nil
}

// settingsFile is the settings file as gopkg.in/yaml.v3 decodes it.
type settingsFile struct {
	// ActionGroups is decoded apart from Viper, which folds the case of every key it reads: an input schema's keys and
	// an MCP server configuration's keep theirs.
	ActionGroups catalog.Settings `yaml:"action_groups"`
	// Others are the settings Viper reads.
	Others map[string]any `yaml:",inline"`
}

func readSettingsFile(path string) (settingsFile, error) {
	var file settingsFile
	document, err := os.ReadFile(path)
	if err != nil {
		return file, errors.New("could not read Action Service settings file")
	}
	decoder := yaml.NewDecoder(bytes.NewReader(document))
	decoder.KnownFields(true)
	if err := decoder.Decode(&file); err != nil {
		return file, fmt.Errorf("settings file: %w", withoutValues(err))
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return file, errors.New("Action Service settings file must contain exactly one YAML document")
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
