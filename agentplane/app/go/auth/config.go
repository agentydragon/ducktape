// Package auth implements Agentplane's browser and workload authentication boundary.
package auth

import (
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	workloadauth "github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/coreos/go-oidc/v3/oidc"
	"golang.org/x/oauth2"
)

const (
	secureCookieName   = "__Host-agentplane_session"
	insecureCookieName = "agentplane_session"
	oidcClientName     = "authentik"
	loginStatePrefix   = "_state_" + oidcClientName + "_"
	pendingLoginTTL    = 10 * time.Minute
)

// OIDCConfig contains the relying-party settings and browser-session policy. Session durations
// retain the Python defaults when zero. The env loader fills TokenRenewBeforeSeconds so its valid
// zero value remains meaningful.
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

// LoadOIDCFromEnv reads AGENTPLANE_OIDC_* settings. It returns nil,nil when the
// feature is wholly unconfigured and an error for partial or invalid configuration.
func LoadOIDCFromEnv() (*OIDCConfig, error) {
	values := []string{
		os.Getenv("AGENTPLANE_OIDC_ISSUER"),
		os.Getenv("AGENTPLANE_OIDC_CLIENT_ID"),
		os.Getenv("AGENTPLANE_OIDC_CLIENT_SECRET"),
		os.Getenv("AGENTPLANE_OIDC_SESSION_SECRET"),
		os.Getenv("AGENTPLANE_OIDC_PUBLIC_BASE_URL"),
	}
	allEmpty := true
	for _, value := range values {
		allEmpty = allEmpty && value == ""
	}
	if allEmpty {
		return nil, nil
	}
	names := []string{"ISSUER", "CLIENT_ID", "CLIENT_SECRET", "SESSION_SECRET", "PUBLIC_BASE_URL"}
	for i, value := range values {
		if value == "" {
			return nil, fmt.Errorf("AGENTPLANE_OIDC_%s is required when OIDC is configured", names[i])
		}
	}
	c := &OIDCConfig{
		Issuer: values[0], ClientID: values[1], ClientSecret: values[2], SessionSecret: values[3], PublicBaseURL: values[4],
	}
	var err error
	if c.SessionMaxSeconds, err = envInt("AGENTPLANE_OIDC_SESSION_MAX_SECONDS", 604800, 1); err != nil {
		return nil, err
	}
	if c.SessionIdleSeconds, err = envInt("AGENTPLANE_OIDC_SESSION_IDLE_SECONDS", 86400, 1); err != nil {
		return nil, err
	}
	if c.SessionActivityStepSeconds, err = envInt("AGENTPLANE_OIDC_SESSION_ACTIVITY_STEP_SECONDS", 300, 1); err != nil {
		return nil, err
	}
	if c.TokenRenewBeforeSeconds, err = envInt("AGENTPLANE_OIDC_TOKEN_RENEW_BEFORE_SECONDS", 30, 0); err != nil {
		return nil, err
	}
	if err := c.validate(); err != nil {
		return nil, err
	}
	return c, nil
}

func envInt(name string, fallback, minimum int) (int, error) {
	text, present := os.LookupEnv(name)
	if !present || text == "" {
		return fallback, nil
	}
	value, err := strconv.Atoi(text)
	if err != nil || value < minimum {
		return 0, fmt.Errorf("%s must be an integer >= %d", name, minimum)
	}
	return value, nil
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
