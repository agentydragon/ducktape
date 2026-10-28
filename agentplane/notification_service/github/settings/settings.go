package settings

import (
	"errors"
	"fmt"
	"math"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"
)

// Settings is the `github` block of the Notification service's settings file. The service config embeds it and
// rejects unknown keys. The private key and webhook secret are Secrets, so a deployment supplies them as
// environment variables rather than in the file.
type Settings struct {
	// APIURL is the trusted GitHub REST API base URL; it receives App JWTs and installation tokens. A path prefix
	// (GitHub Enterprise's /api/v3) is allowed.
	APIURL                string  `mapstructure:"api_url"`
	RequestTimeoutSeconds float64 `mapstructure:"request_timeout_s"`
	// APIVersion is the X-GitHub-Api-Version date. YAML needs it quoted, or it reads as a timestamp.
	APIVersion string `mapstructure:"api_version"`
	// AppID is the public numeric GitHub App ID, not a secret.
	AppID int64 `mapstructure:"app_id"`
	// PrivateKey is the App's PEM private key.
	PrivateKey string `mapstructure:"private_key"`
	// WebhookSecret is the HMAC key GitHub signs webhook deliveries with.
	WebhookSecret string `mapstructure:"webhook_secret"`
	// FreshnessSeconds bounds the age of shared GitHub access validation and subject repair.
	FreshnessSeconds int `mapstructure:"freshness_seconds"`
	// MaxBodyBytes bounds a raw webhook request body.
	MaxBodyBytes int `mapstructure:"max_body_bytes"`
	// WebhookConcurrency caps concurrent webhook requests per replica, held through durable commit; saturation
	// returns 503.
	WebhookConcurrency int `mapstructure:"webhook_concurrency"`
}

// DefaultSettings is Settings with every default applied: the settings with no default are AppID, PrivateKey and
// WebhookSecret, which Validate requires.
func DefaultSettings() Settings {
	return Settings{
		APIURL:                "https://api.github.com",
		RequestTimeoutSeconds: 5,
		APIVersion:            "2022-11-28",
		FreshnessSeconds:      600,
		MaxBodyBytes:          1024 * 1024,
		WebhookConcurrency:    8,
	}
}

// Validate reports the first setting out of range. Its errors name the setting, never its value.
func (s Settings) Validate() error {
	if err := validateAPIURL(s.APIURL); err != nil {
		return err
	}
	if !(s.RequestTimeoutSeconds > 0) || math.IsInf(s.RequestTimeoutSeconds, 0) {
		return errors.New("github.request_timeout_s must be a finite number above 0")
	}
	if version, err := time.Parse(time.DateOnly, s.APIVersion); err != nil || version.Format(time.DateOnly) != s.APIVersion {
		return errors.New("github.api_version must be a YYYY-MM-DD date")
	}
	if s.AppID <= 0 {
		return errors.New("github.app_id must be above 0")
	}
	if s.PrivateKey == "" {
		return errors.New("github.private_key is required")
	}
	if utf8.RuneCountInString(s.WebhookSecret) < 16 {
		return errors.New("github.webhook_secret must be at least 16 characters")
	}
	for _, bound := range []struct {
		name            string
		value, low, max int
	}{
		{"freshness_seconds", s.FreshnessSeconds, 60, 3600},
		{"max_body_bytes", s.MaxBodyBytes, 1024, 25 * 1024 * 1024},
		{"webhook_concurrency", s.WebhookConcurrency, 1, 64},
	} {
		if bound.value < bound.low || bound.value > bound.max {
			return fmt.Errorf("github.%s must be between %d and %d", bound.name, bound.low, bound.max)
		}
	}
	return nil
}

func validateAPIURL(raw string) error {
	parsed, err := url.Parse(raw)
	// A query or fragment, even an empty one, would be dropped when paths are appended.
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" ||
		parsed.User != nil || strings.ContainsAny(raw, "?#") {
		return errors.New("github.api_url must be an http or https URL without credentials, query or fragment")
	}
	return nil
}

// RequestTimeout saturates a huge setting instead of overflowing time.Duration.
func (s Settings) RequestTimeout() time.Duration {
	return time.Duration(min(s.RequestTimeoutSeconds, math.MaxInt32) * float64(time.Second))
}
