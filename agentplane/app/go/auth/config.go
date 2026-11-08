// Package auth implements Agentplane's browser and workload authentication boundary.
package auth

import (
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	workloadauth "github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/coreos/go-oidc/v3/oidc"
	"golang.org/x/oauth2"
)

const (
	secureCookieName   = "__Host-agentplane_session"
	insecureCookieName = "agentplane_session"
	pendingLoginTTL    = 10 * time.Minute
)

// OIDCConfig contains the relying-party settings and browser-session policy. Session durations
// use their configured defaults. TokenRenewBeforeSeconds intentionally accepts zero.
type OIDCConfig struct {
	Issuer                     string
	ClientID                   string
	ClientSecret               string
	SessionSecret              string
	PublicBaseURL              string
	SessionMaxSeconds          int
	SessionIdleSeconds         int
	SessionActivityStepSeconds int
	TokenRenewBeforeSeconds    int
}

// OIDCSettings contains the non-secret relying-party settings authored in the mounted app YAML.
// Pointer durations distinguish omitted values (defaults) from explicit zero for token renewal.
type OIDCSettings struct {
	Issuer                     string `mapstructure:"issuer"`
	PublicBaseURL              string `mapstructure:"public_base_url"`
	SessionMaxSeconds          *int   `mapstructure:"session_max_seconds"`
	SessionIdleSeconds         *int   `mapstructure:"session_idle_seconds"`
	SessionActivityStepSeconds *int   `mapstructure:"session_activity_step_seconds"`
	TokenRenewBeforeSeconds    *int   `mapstructure:"token_renew_before_seconds"`
}

// RedirectURI is the fixed public callback registered with the identity provider.
func (c OIDCConfig) RedirectURI() string {
	return strings.TrimRight(c.PublicBaseURL, "/") + "/auth/callback"
}

// CookieName is __Host-prefixed whenever the public origin uses HTTPS.
func (c OIDCConfig) CookieName() string {
	if strings.HasPrefix(c.PublicBaseURL, "https://") {
		return secureCookieName
	}
	return insecureCookieName
}

// Secure reports whether the browser cookie must be sent only over HTTPS.
func (c OIDCConfig) Secure() bool { return strings.HasPrefix(c.PublicBaseURL, "https://") }

// LoadOIDC overlays explicit credential inputs onto the static settings file section. It returns
// nil,nil when the feature is wholly unconfigured and an error for partial or invalid configuration.
func LoadOIDC(settings *OIDCSettings) (*OIDCConfig, error) {
	secretNames := []string{
		"AGENTPLANE_OIDC_CLIENT_ID", "AGENTPLANE_OIDC_CLIENT_SECRET", "AGENTPLANE_OIDC_SESSION_SECRET",
	}
	secrets := make([]string, len(secretNames))
	anySecretPresent := false
	for index, name := range secretNames {
		value, present := os.LookupEnv(name)
		secrets[index] = value
		anySecretPresent = anySecretPresent || present
	}
	if settings == nil && !anySecretPresent {
		return nil, nil
	}
	if settings == nil {
		return nil, errors.New("OIDC Secret inputs require the oidc settings section in AGENTPLANE_CONFIG_FILE")
	}
	for index, value := range secrets {
		if value == "" {
			return nil, fmt.Errorf("%s is required when oidc is configured", secretNames[index])
		}
	}
	c := &OIDCConfig{
		Issuer: settings.Issuer, ClientID: secrets[0], ClientSecret: secrets[1], SessionSecret: secrets[2],
		PublicBaseURL:              settings.PublicBaseURL,
		SessionMaxSeconds:          intSetting(settings.SessionMaxSeconds, 604800),
		SessionIdleSeconds:         intSetting(settings.SessionIdleSeconds, 86400),
		SessionActivityStepSeconds: intSetting(settings.SessionActivityStepSeconds, 300),
		TokenRenewBeforeSeconds:    intSetting(settings.TokenRenewBeforeSeconds, 30),
	}
	if c.SessionMaxSeconds <= 0 {
		return nil, errors.New("oidc.session_max_seconds must be positive")
	}
	if c.SessionIdleSeconds <= 0 {
		return nil, errors.New("oidc.session_idle_seconds must be positive")
	}
	if c.SessionActivityStepSeconds <= 0 {
		return nil, errors.New("oidc.session_activity_step_seconds must be positive")
	}
	if c.TokenRenewBeforeSeconds < 0 {
		return nil, errors.New("oidc.token_renew_before_seconds must be non-negative")
	}
	if err := c.validate(); err != nil {
		return nil, err
	}
	return c, nil
}

func intSetting(value *int, fallback int) int {
	if value == nil {
		return fallback
	}
	return *value
}

func (c OIDCConfig) validate() error {
	if c.Issuer == "" || c.ClientID == "" || c.ClientSecret == "" || c.SessionSecret == "" || c.PublicBaseURL == "" {
		return errors.New("OIDC issuer, client ID, client secret, session secret, and public base URL are required")
	}
	issuer, err := url.Parse(c.Issuer)
	if err != nil || issuer.Scheme != "https" || issuer.Host == "" {
		return errors.New("OIDC issuer must be an absolute HTTPS URL")
	}
	base, err := url.Parse(c.PublicBaseURL)
	if err != nil || (base.Scheme != "http" && base.Scheme != "https") || base.Host == "" || base.RawQuery != "" || base.Fragment != "" {
		return errors.New("OIDC public base URL must be an HTTP(S) origin without query or fragment")
	}
	if c.SessionMaxSeconds < 0 || c.SessionIdleSeconds < 0 || c.SessionActivityStepSeconds < 0 || c.TokenRenewBeforeSeconds < 0 {
		return errors.New("OIDC session settings must be non-negative")
	}
	return nil
}

func (c OIDCConfig) withDefaults() OIDCConfig {
	if c.SessionMaxSeconds == 0 {
		c.SessionMaxSeconds = 604800
	}
	if c.SessionIdleSeconds == 0 {
		c.SessionIdleSeconds = 86400
	}
	if c.SessionActivityStepSeconds == 0 {
		c.SessionActivityStepSeconds = 300
	}
	return c
}

// Config wires persistence, OIDC, and Kubernetes TokenReview into an Auth instance.
type Config struct {
	OIDC                  *OIDCConfig
	Database              *sql.DB
	TokenReviews          workloadauth.TokenReviews
	TokenAudience         string
	TokenSubjects         []string
	StoreFederationTokens bool
	HTTPClient            *http.Client
	SessionChanges        ChangeSource
}

type openID struct {
	provider *oidc.Provider
	verifier *oidc.IDTokenVerifier
	oauth    oauth2.Config
}
