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

	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	sharedconfig "github.com/agentydragon/ducktape/agentplane/settings/go"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
	"gopkg.in/yaml.v3"
)

const settingPrefix = "AGENTPLANE_MCP_EDGE_"

// config is the standalone edge's deployment contract. Secret values come from environment variables.
type config struct {
	DatabaseURL                    string   `mapstructure:"database_url" yaml:"database_url"`
	ActionsTarget                  string   `mapstructure:"actions_target" yaml:"actions_target"`
	Host                           string   `mapstructure:"host" yaml:"host"`
	Port                           int      `mapstructure:"port" yaml:"port"`
	CallerServiceAccountNamespaces []string `mapstructure:"caller_service_account_namespaces" yaml:"caller_service_account_namespaces"`
	// MintableAccounts is the explicit ownership scope granted to this edge's TokenRequest RBAC.
	MintableAccounts  []auth.Account           `mapstructure:"mintable_accounts" yaml:"mintable_accounts"`
	OperatorOIDC      api.OperatorOIDCSettings `mapstructure:"operator_oidc" yaml:"operator_oidc"`
	OAuth             *oauthSettings           `mapstructure:"oauth" yaml:"oauth"`
	MaxWaitSeconds    float64                  `mapstructure:"max_wait_seconds" yaml:"max_wait_seconds"`
	DirectWaitSeconds float64                  `mapstructure:"direct_wait_seconds" yaml:"direct_wait_seconds"`
}

type oauthSettings struct {
	// CLEANUP(added 2026-10-23): Drop these legacy FastMCP keys, which loadConfig's UnmarshalExact would otherwise
	//   reject, once the tofu controller has applied #PR4 and the agentplane-mcp-oauth Secret's oauth no longer has them.
	ConfigURL         string `mapstructure:"config_url" yaml:"config_url"`
	JWTSigningKeyFile string `mapstructure:"jwt_signing_key_file" yaml:"jwt_signing_key_file"`
	EncryptionKeyFile string `mapstructure:"encryption_key_file" yaml:"encryption_key_file"`
	// The edge's active external OAuth settings follow.
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
		Host: "0.0.0.0", Port: 8080, MaxWaitSeconds: 30, DirectWaitSeconds: 30,
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
	if c.Port < 1 || c.Port > 65535 {
		return errors.New("port must be between 1 and 65535")
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

func (c config) address() string { return fmt.Sprintf("%s:%d", c.Host, c.Port) }
