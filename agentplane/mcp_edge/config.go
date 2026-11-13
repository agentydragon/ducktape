package main

import (
	"errors"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"

	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"gopkg.in/yaml.v3"
	"k8s.io/apimachinery/pkg/util/validation"
)

const (
	settingPrefix  = "AGENTPLANE_MCP_EDGE_"
	databaseURLEnv = settingPrefix + "DATABASE_URL"
)

// config is the standalone edge's non-secret deployment contract. DatabaseURL is supplied separately from a Secret.
type config struct {
	ActionsTLS    *environment_runtime_transport.ClientTLS `yaml:"actions_tls,omitempty"`
	DatabaseURL   string                                   `mapstructure:"-" yaml:"-"`
	ActionsTarget string                                   `yaml:"actions_target"`
	// PolicyNamespace holds live Identities; ActionsTokenFile is the rotating Action-audience token.
	PolicyNamespace  string `yaml:"policy_namespace"`
	ActionsTokenFile string `yaml:"actions_token_file"`
	Host             string `yaml:"host"`
	// Port is the gRPC listener's: ConnectionService and health. HTTPPort is the HTTP listener's: the facade, /mcp,
	// OAuth and the metrics.
	Port              int                      `yaml:"port"`
	HTTPPort          int                      `yaml:"http_port"`
	OperatorOIDC      api.OperatorOIDCSettings `yaml:"operator_oidc"`
	OAuthFile         string                   `yaml:"oauth_file,omitempty"`
	OAuth             *oauthSettings           `yaml:"oauth,omitempty"`
	MaxWaitSeconds    float64                  `yaml:"max_wait_seconds"`
	DirectWaitSeconds float64                  `yaml:"direct_wait_seconds"`
}

type oauthSettings struct {
	UpstreamClientID         string `yaml:"upstream_client_id"`
	UpstreamClientSecretFile string `yaml:"upstream_client_secret_file"`
	BaseURL                  string `yaml:"base_url"`
	IntegrationAppURL        string `yaml:"integration_app_url"`
	UpstreamIssuer           string `yaml:"upstream_issuer"`
	UpstreamSubject          string `yaml:"upstream_subject"`
	ApprovingOperator        struct {
		Issuer  string `yaml:"issuer"`
		Subject string `yaml:"subject"`
	} `yaml:"approving_operator"`
}

func loadConfig() (config, error) {
	c := config{
		Host: "0.0.0.0", Port: 8080, HTTPPort: 8081, MaxWaitSeconds: 30, DirectWaitSeconds: 30,
		OperatorOIDC: api.DefaultOperatorOIDCSettings(),
	}
	path := os.Getenv(settingPrefix + "CONFIG_FILE")
	if path == "" {
		return config{}, errors.New("MCP edge settings file path is required")
	}
	file, err := os.Open(path)
	if err != nil {
		return config{}, errors.New("could not read MCP edge settings file")
	}
	defer file.Close()
	decoder := yaml.NewDecoder(file)
	decoder.KnownFields(true)
	if err := decoder.Decode(&c); err != nil {
		return config{}, errors.New("invalid MCP edge settings file")
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return config{}, errors.New("invalid MCP edge settings file")
	}
	if c.OAuthFile != "" {
		if c.OAuth != nil {
			return config{}, errors.New("oauth_file and inline oauth settings are mutually exclusive")
		}
		c.OAuth, err = loadOAuthSettings(c.OAuthFile)
		if err != nil {
			return config{}, errors.New("invalid MCP edge OAuth settings file")
		}
	}
	c.DatabaseURL = os.Getenv(databaseURLEnv)
	if err := c.validate(); err != nil {
		return config{}, errors.New("invalid MCP edge settings")
	}
	return c, nil
}

func loadOAuthSettings(path string) (*oauthSettings, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, errors.New("could not read MCP edge OAuth settings file")
	}
	defer file.Close()
	decoder := yaml.NewDecoder(file)
	decoder.KnownFields(true)
	var settings oauthSettings
	if err := decoder.Decode(&settings); err != nil {
		return nil, errors.New("invalid MCP edge OAuth settings file")
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, errors.New("invalid MCP edge OAuth settings file")
	}
	return &settings, nil
}

func (c config) validate() error {
	for _, required := range []struct{ name, value string }{{"database_url", c.DatabaseURL}, {"actions_target", c.ActionsTarget}, {"host", c.Host}} {
		if strings.TrimSpace(required.value) == "" {
			return errors.New(required.name + " is required")
		}
	}
	if len(validation.IsDNS1123Label(c.PolicyNamespace)) > 0 {
		return errors.New("policy_namespace must be a namespace name")
	}
	for name, port := range map[string]int{"port": c.Port, "http_port": c.HTTPPort} {
		if port < 1 || port > 65535 {
			return errors.New(name + " must be between 1 and 65535")
		}
	}
	if c.Port == c.HTTPPort {
		return errors.New("port and http_port must differ")
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
	if strings.TrimSpace(c.ActionsTokenFile) == "" {
		return errors.New("actions_token_file is required when oauth is enabled")
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

func (c config) grpcAddress() string { return fmt.Sprintf("%s:%d", c.Host, c.Port) }

func (c config) httpAddress() string { return fmt.Sprintf("%s:%d", c.Host, c.HTTPPort) }
