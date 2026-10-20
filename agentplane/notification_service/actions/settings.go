package actions

import (
	"errors"
	"math"
	"net/url"
	"strings"
	"time"
)

// Settings is the `actions` block of the Notification service's settings file: the service config embeds it
// and rejects unknown keys, and the cdk8s manifests set `url` and `token_file`.
type Settings struct {
	// URL is the base URL of the Action Service's request and event read API. A path prefix is allowed.
	URL string `mapstructure:"url"`
	// TokenFile is the projected ServiceAccount token the Action Service authenticates the service by. It is
	// read again for each request, to follow its rotation.
	TokenFile             string  `mapstructure:"token_file"`
	RequestTimeoutSeconds float64 `mapstructure:"request_timeout_s"`
}

// DefaultSettings is Settings with every default applied: URL and TokenFile have none, and Validate requires
// them.
func DefaultSettings() Settings { return Settings{RequestTimeoutSeconds: 5} }

// Validate reports the first setting out of range. Its errors name the setting, never its value.
func (s Settings) Validate() error {
	parsed, err := url.Parse(s.URL)
	// A query or fragment, even an empty one, would be dropped when paths are appended.
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" ||
		parsed.User != nil || strings.ContainsAny(s.URL, "?#") {
		return errors.New("actions.url must be an http or https URL without credentials, query or fragment")
	}
	if s.TokenFile == "" {
		return errors.New("actions.token_file is required")
	}
	if !(s.RequestTimeoutSeconds > 0) || math.IsInf(s.RequestTimeoutSeconds, 0) {
		return errors.New("actions.request_timeout_s must be a finite number above 0")
	}
	return nil
}

// requestTimeout saturates a huge setting instead of overflowing time.Duration.
func (s Settings) requestTimeout() time.Duration {
	return time.Duration(min(s.RequestTimeoutSeconds, math.MaxInt32) * float64(time.Second))
}
