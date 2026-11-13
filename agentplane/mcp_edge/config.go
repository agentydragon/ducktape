package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"

	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	sharedconfig "github.com/agentydragon/ducktape/agentplane/settings"
	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
	"gopkg.in/yaml.v3"
	"k8s.io/apimachinery/pkg/util/validation"
)

const settingPrefix = "AGENTPLANE_MCP_EDGE_"

// config is the standalone edge's deployment contract. Secret values come from environment variables.
type config struct {
	ActionsTLS    *environment_runtime_transport.ClientTLS `mapstructure:"actions_tls" json:"actions_tls,omitempty"`
	DatabaseURL   string                                   `mapstructure:"database_url" yaml:"database_url"`
	ActionsTarget string                                   `mapstructure:"actions_target" yaml:"actions_target"`
	// PolicyNamespace and ActionsTokenFile are accepted ahead of the MCP Identity cutover image.
	PolicyNamespace  string `mapstructure:"policy_namespace" yaml:"policy_namespace"`
	ActionsTokenFile string `mapstructure:"actions_token_file" yaml:"actions_token_file"`
	Host             string `mapstructure:"host" yaml:"host"`
	// Port is the gRPC listener's: ConnectionService and health. HTTPPort is the HTTP listener's: the facade, /mcp,
	// OAuth and the metrics.
	Port                           int      `mapstructure:"port" yaml:"port"`
	HTTPPort                       int      `mapstructure:"http_port" yaml:"http_port"`
	CallerServiceAccountNamespaces []string `mapstructure:"caller_service_account_namespaces" yaml:"caller_service_account_namespaces"`
	// MintableAccounts is the explicit ownership scope granted to this edge's TokenRequest RBAC.
	MintableAccounts []auth.Account `mapstructure:"mintable_accounts" yaml:"mintable_accounts"`
	// ConnectionIdentities is nil while Connections bind only to mintable_accounts.
	ConnectionIdentities *connectionIdentities    `mapstructure:"connection_identities" yaml:"connection_identities"`
	OperatorOIDC         api.OperatorOIDCSettings `mapstructure:"operator_oidc" yaml:"operator_oidc"`
	OAuth                *oauthSettings           `mapstructure:"oauth" yaml:"oauth"`
	MaxWaitSeconds       float64                  `mapstructure:"max_wait_seconds" yaml:"max_wait_seconds"`
	DirectWaitSeconds    float64                  `mapstructure:"direct_wait_seconds" yaml:"direct_wait_seconds"`
}

// connectionIdentities are the Identities a Connection may be bound to, as mintable_accounts are the ServiceAccounts.
// Staged for IDENTITY_CUTOVER (agentplane/plans/task_dag.md), which sets it and makes the edge bind Connections to
// these Identities; until then the edge validates it and nothing reads it.
type connectionIdentities struct {
	// PolicyNamespace holds the Identity objects.
	PolicyNamespace string `mapstructure:"policy_namespace" yaml:"policy_namespace"`
	// Names are Identity names in PolicyNamespace; duplicates name the same Identity.
	Names []string `mapstructure:"names" yaml:"names"`
}

type oauthSettings struct {
	UpstreamClientID         string `mapstructure:"upstream_client_id" yaml:"upstream_client_id"`
	UpstreamClientSecretFile string `mapstructure:"upstream_client_secret_file" yaml:"upstream_client_secret_file"`
	BaseURL                  string `mapstructure:"base_url" yaml:"base_url"`
	IntegrationAppURL        string `mapstructure:"integration_app_url" yaml:"integration_app_url"`
	UpstreamIssuer           string `mapstructure:"upstream_issuer" yaml:"upstream_issuer"`
	UpstreamSubject          string `mapstructure:"upstream_subject" yaml:"upstream_subject"`
	ApprovingOperator        struct {
		Issuer  string `mapstructure:"issuer" yaml:"issuer"`
		Subject string `mapstructure:"subject" yaml:"subject"`
	} `mapstructure:"approving_operator" yaml:"approving_operator"`
}

func loadConfig() (config, error) {
	c := config{
		Host: "0.0.0.0", Port: 8080, HTTPPort: 8081, MaxWaitSeconds: 30, DirectWaitSeconds: 30,
		OperatorOIDC: api.DefaultOperatorOIDCSettings(),
	}
	settings := viper.NewWithOptions(viper.ExperimentalBindStruct(), viper.EnvKeyReplacer(strings.NewReplacer(".", "__")))
	settings.SetEnvPrefix("AGENTPLANE_MCP_EDGE")
	settings.AutomaticEnv()
	if path := os.Getenv(settingPrefix + "CONFIG_FILE"); path != "" {
		file, err := os.Open(path)
		if err != nil {
			return config{}, errors.New("could not read MCP edge settings file")
		}
		defer file.Close()
		var values map[string]any
		decoder := yaml.NewDecoder(file)
		if err := decoder.Decode(&values); err != nil {
			return config{}, errors.New("invalid MCP edge settings file")
		}
		if err := decoder.Decode(new(any)); err != io.EOF {
			return config{}, errors.New("invalid MCP edge settings file")
		}
		if err := settings.MergeConfigMap(values); err != nil {
			return config{}, errors.New("invalid MCP edge settings file")
		}
	}
	if raw := os.Getenv(settingPrefix + "OAUTH"); raw != "" {
		var values map[string]any
		if err := json.Unmarshal([]byte(raw), &values); err != nil || values == nil {
			return config{}, errors.New("oauth must be a JSON object")
		}
		sharedconfig.SetJSONLeaves(settings, "oauth", values, settingPrefix)
	}
	if raw := os.Getenv(settingPrefix + "CALLER_SERVICE_ACCOUNT_NAMESPACES"); raw != "" {
		var namespaces []string
		if err := json.Unmarshal([]byte(raw), &namespaces); err != nil {
			return config{}, errors.New("caller_service_account_namespaces must be a JSON array")
		}
		settings.Set("caller_service_account_namespaces", namespaces)
	}
	if raw := os.Getenv(settingPrefix + "MINTABLE_ACCOUNTS"); raw != "" {
		var accounts []string
		if err := json.Unmarshal([]byte(raw), &accounts); err != nil {
			return config{}, errors.New("mintable_accounts must be a JSON array")
		}
		settings.Set("mintable_accounts", accounts)
	}
	if err := settings.UnmarshalExact(&c, viper.DecodeHook(mapstructure.TextUnmarshallerHookFunc())); err != nil {
		return config{}, errors.New("invalid MCP edge settings")
	}
	return c, c.validate()
}

func (c config) validate() error {
	for _, required := range []struct{ name, value string }{{"database_url", c.DatabaseURL}, {"actions_target", c.ActionsTarget}, {"host", c.Host}} {
		if strings.TrimSpace(required.value) == "" {
			return errors.New(required.name + " is required")
		}
	}
	for name, port := range map[string]int{"port": c.Port, "http_port": c.HTTPPort} {
		if port < 1 || port > 65535 {
			return errors.New(name + " must be between 1 and 65535")
		}
	}
	if c.Port == c.HTTPPort {
		return errors.New("port and http_port must differ")
	}
	if len(c.CallerServiceAccountNamespaces) == 0 {
		return errors.New("caller_service_account_namespaces must list at least one namespace")
	}
	for _, namespace := range c.CallerServiceAccountNamespaces {
		if namespace == "" {
			return errors.New("caller_service_account_namespaces contains an empty namespace")
		}
	}
	for _, account := range c.MintableAccounts {
		if account.Namespace == "" || account.Name == "" {
			return errors.New("mintable_accounts requires namespace/name")
		}
		if !slices.Contains(c.CallerServiceAccountNamespaces, account.Namespace) {
			return errors.New("mintable_accounts must be in a caller namespace")
		}
	}
	if err := c.ConnectionIdentities.validate(); err != nil {
		return err
	}
	for _, seconds := range []float64{c.MaxWaitSeconds, c.DirectWaitSeconds} {
		if seconds < 0 || math.IsNaN(seconds) || math.IsInf(seconds, 0) {
			return errors.New("MCP wait seconds must be finite and nonnegative")
		}
	}
	if err := c.OperatorOIDC.Validate(); err != nil {
		return err
	}
	if c.OAuth == nil {
		return nil
	}
	if len(c.MintableAccounts) == 0 {
		return errors.New("mintable_accounts must list at least one account when oauth is enabled")
	}
	o := c.OAuth
	for _, value := range []string{o.UpstreamClientID, o.UpstreamClientSecretFile, o.UpstreamSubject, o.ApprovingOperator.Issuer, o.ApprovingOperator.Subject} {
		if strings.TrimSpace(value) == "" {
			return errors.New("oauth requires client, secret file, upstream subject and approving operator")
		}
	}
	for _, value := range []string{o.BaseURL, o.IntegrationAppURL, o.UpstreamIssuer, o.ApprovingOperator.Issuer} {
		parsed, err := url.Parse(value)
		if err != nil || parsed.Host == "" || parsed.User != nil || parsed.Fragment != "" || (parsed.Scheme != "https" && !(parsed.Scheme == "http" && (parsed.Hostname() == "localhost" || parsed.Hostname() == "127.0.0.1" || parsed.Hostname() == "::1"))) {
			return errors.New("oauth endpoints must use https or loopback http")
		}
	}
	issuer, _ := url.Parse(o.BaseURL)
	if issuer.Path != "" || issuer.RawQuery != "" {
		return errors.New("oauth.base_url must be an origin")
	}
	return nil
}

func (i *connectionIdentities) validate() error {
	if i == nil {
		return nil
	}
	if len(validation.IsDNS1123Label(i.PolicyNamespace)) > 0 {
		return errors.New("connection_identities.policy_namespace must be a namespace name")
	}
	if len(i.Names) == 0 {
		return errors.New("connection_identities.names must list at least one Identity")
	}
	for _, name := range i.Names {
		if len(validation.IsDNS1123Subdomain(name)) > 0 {
			return fmt.Errorf("connection_identities.names must be Identity object names: %q", name)
		}
	}
	return nil
}

func (c config) grpcAddress() string { return fmt.Sprintf("%s:%d", c.Host, c.Port) }

func (c config) httpAddress() string { return fmt.Sprintf("%s:%d", c.Host, c.HTTPPort) }
