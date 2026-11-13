package settings

import (
	"math"
	"strings"
	"testing"

	"github.com/go-viper/mapstructure/v2"
)

func validSettings() Settings {
	settings := DefaultSettings()
	settings.AppID = 42
	settings.PrivateKey = "test-private-key"
	settings.WebhookSecret = "test-webhook-secret!"
	return settings
}

func TestSettingsAcceptTheirDefaultsAndBounds(t *testing.T) {
	if err := validSettings().Validate(); err != nil {
		t.Fatal(err)
	}
	for name, edit := range map[string]func(*Settings){
		"a path prefix and plain HTTP":    func(s *Settings) { s.APIURL = "http://github.test/api/v3" },
		"a trailing slash":                func(s *Settings) { s.APIURL = "https://github.test/api/v3/" },
		"the lowest bounds":               func(s *Settings) { s.FreshnessSeconds, s.MaxBodyBytes, s.WebhookConcurrency = 60, 1024, 1 },
		"the highest bounds":              func(s *Settings) { s.FreshnessSeconds, s.MaxBodyBytes, s.WebhookConcurrency = 3600, 25<<20, 64 },
		"a fractional timeout":            func(s *Settings) { s.RequestTimeoutSeconds = 0.25 },
		"a timeout past Duration's range": func(s *Settings) { s.RequestTimeoutSeconds = 1e300 },
		"the shortest webhook secret":     func(s *Settings) { s.WebhookSecret = strings.Repeat("é", 16) },
	} {
		t.Run(name, func(t *testing.T) {
			settings := validSettings()
			edit(&settings)
			if err := settings.Validate(); err != nil {
				t.Error(err)
			}
			if settings.RequestTimeout() <= 0 {
				t.Errorf("request timeout %s", settings.RequestTimeout())
			}
		})
	}
}

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	const secret = "never-print-this"
	for name, edit := range map[string]func(*Settings){
		"no scheme":                      func(s *Settings) { s.APIURL = "not-a-url" },
		"another scheme":                 func(s *Settings) { s.APIURL = "ftp://github.test" },
		"no host":                        func(s *Settings) { s.APIURL = "https:///api" },
		"credentials":                    func(s *Settings) { s.APIURL = "https://user:" + secret + "@github.test" },
		"a query":                        func(s *Settings) { s.APIURL = "https://github.test?token=" + secret },
		"an empty query":                 func(s *Settings) { s.APIURL = "https://github.test?" },
		"a fragment":                     func(s *Settings) { s.APIURL = "https://github.test#" + secret },
		"no timeout":                     func(s *Settings) { s.RequestTimeoutSeconds = 0 },
		"a negative timeout":             func(s *Settings) { s.RequestTimeoutSeconds = -1 },
		"an infinite timeout":            func(s *Settings) { s.RequestTimeoutSeconds = math.Inf(1) },
		"a timeout that is not a number": func(s *Settings) { s.RequestTimeoutSeconds = math.NaN() },
		"a version that is not a day":    func(s *Settings) { s.APIVersion = "2022-02-30" },
		"an unpadded version":            func(s *Settings) { s.APIVersion = "2022-2-3" },
		"no version":                     func(s *Settings) { s.APIVersion = "" },
		"no app":                         func(s *Settings) { s.AppID = 0 },
		"no private key":                 func(s *Settings) { s.PrivateKey = "" },
		"a short webhook secret":         func(s *Settings) { s.WebhookSecret = secret[:15] },
		"too little freshness":           func(s *Settings) { s.FreshnessSeconds = 59 },
		"too much freshness":             func(s *Settings) { s.FreshnessSeconds = 3601 },
		"a tiny body limit":              func(s *Settings) { s.MaxBodyBytes = 1023 },
		"a huge body limit":              func(s *Settings) { s.MaxBodyBytes = 25<<20 + 1 },
		"no webhook concurrency":         func(s *Settings) { s.WebhookConcurrency = 0 },
		"too much concurrency":           func(s *Settings) { s.WebhookConcurrency = 65 },
	} {
		t.Run(name, func(t *testing.T) {
			settings := validSettings()
			edit(&settings)
			err := settings.Validate()
			if err == nil {
				t.Fatal("settings were accepted")
			}
			if strings.Contains(err.Error(), secret) {
				t.Errorf("error echoes a value: %v", err)
			}
		})
	}
}

// The containing service config decodes this struct from YAML and fills credentials explicitly from Secret inputs.
func TestStaticSettingsDecodeAndSecretFieldsAreNotFileSettings(t *testing.T) {
	file := map[string]any{
		"api_url": "https://github.test/api/v3", "request_timeout_s": 2.5, "api_version": "2026-01-01",
		"app_id": 42, "freshness_seconds": 120, "max_body_bytes": 2048, "webhook_concurrency": 4,
	}
	settings := DefaultSettings()
	decoder, err := mapstructure.NewDecoder(&mapstructure.DecoderConfig{
		Result: &settings, TagName: "mapstructure", ErrorUnused: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(file); err != nil {
		t.Fatal(err)
	}
	want := DefaultSettings()
	want.APIURL, want.RequestTimeoutSeconds, want.APIVersion, want.AppID = "https://github.test/api/v3", 2.5, "2026-01-01", 42
	want.FreshnessSeconds, want.MaxBodyBytes, want.WebhookConcurrency = 120, 2048, 4
	if settings != want {
		t.Errorf("settings = %+v, want %+v", settings, want)
	}
	for _, secretKey := range []string{"private_key", "webhook_secret"} {
		decoder, err := mapstructure.NewDecoder(&mapstructure.DecoderConfig{
			Result: &settings, TagName: "mapstructure", ErrorUnused: true,
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := decoder.Decode(map[string]any{secretKey: "never-print-this"}); err == nil {
			t.Errorf("accepted %s from static settings", secretKey)
		}
	}
}
