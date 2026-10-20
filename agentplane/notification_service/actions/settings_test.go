package actions

import (
	"math"
	"strings"
	"testing"
	"time"

	"github.com/spf13/viper"
)

func validSettings() Settings {
	settings := DefaultSettings()
	settings.URL = "http://actions.test:8080"
	settings.TokenFile = "/var/run/secrets/notifications/actions"
	return settings
}

func TestSettingsAcceptTheirDefaultsAndBounds(t *testing.T) {
	if err := validSettings().Validate(); err != nil {
		t.Fatal(err)
	}
	for name, edit := range map[string]func(*Settings){
		"HTTPS":                           func(s *Settings) { s.URL = "https://actions.test" },
		"a path prefix":                   func(s *Settings) { s.URL = "http://actions.test/api" },
		"a trailing slash":                func(s *Settings) { s.URL = "http://actions.test/api/" },
		"a fractional timeout":            func(s *Settings) { s.RequestTimeoutSeconds = 0.25 },
		"a timeout past Duration's range": func(s *Settings) { s.RequestTimeoutSeconds = 1e300 },
	} {
		t.Run(name, func(t *testing.T) {
			settings := validSettings()
			edit(&settings)
			if err := settings.Validate(); err != nil {
				t.Error(err)
			}
			if settings.requestTimeout() <= 0 {
				t.Errorf("request timeout %s", settings.requestTimeout())
			}
		})
	}
}

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	const secret = "never-print-this"
	for name, edit := range map[string]func(*Settings){
		"no URL":             func(s *Settings) { s.URL = "" },
		"no scheme":          func(s *Settings) { s.URL = "actions.test" },
		"another scheme":     func(s *Settings) { s.URL = "ftp://actions.test" },
		"no host":            func(s *Settings) { s.URL = "http:///api" },
		"credentials":        func(s *Settings) { s.URL = "http://user:" + secret + "@actions.test" },
		"a query":            func(s *Settings) { s.URL = "http://actions.test?token=" + secret },
		"an empty query":     func(s *Settings) { s.URL = "http://actions.test?" },
		"a fragment":         func(s *Settings) { s.URL = "http://actions.test#" + secret },
		"no token file":      func(s *Settings) { s.TokenFile = "" },
		"no timeout":         func(s *Settings) { s.RequestTimeoutSeconds = 0 },
		"a negative timeout": func(s *Settings) { s.RequestTimeoutSeconds = -1 },
		"an infinite timeout": func(s *Settings) {
			s.RequestTimeoutSeconds = math.Inf(1)
		},
		"a timeout that is not a number": func(s *Settings) { s.RequestTimeoutSeconds = math.NaN() },
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

// The manifests set `actions.url` and `actions.token_file`; the service config that embeds Settings rejects
// unknown keys.
func TestSettingsDecodeTheKeysTheManifestsSet(t *testing.T) {
	decode := func(file string) (Settings, error) {
		loaded := viper.NewWithOptions(viper.ExperimentalBindStruct())
		loaded.SetConfigType("yaml")
		if err := loaded.ReadConfig(strings.NewReader(file)); err != nil {
			t.Fatal(err)
		}
		config := struct {
			Actions Settings `mapstructure:"actions"`
		}{Actions: DefaultSettings()}
		err := loaded.UnmarshalExact(&config)
		return config.Actions, err
	}

	settings, err := decode("actions:\n  url: http://actions.test:8080\n  token_file: /var/run/secrets/notifications/actions\n")
	if err != nil || settings != validSettings() {
		t.Errorf("settings = %+v, %v", settings, err)
	}
	settings, err = decode("actions:\n  url: http://actions.test\n  token_file: /token\n  request_timeout_s: 2.5\n")
	if err != nil || settings.requestTimeout() != 2500*time.Millisecond {
		t.Errorf("settings with a timeout = %+v, %v", settings, err)
	}
	if _, err = decode("actions:\n  url: http://actions.test\n  token_file: /token\n  unknown: 1\n"); err == nil {
		t.Error("an unknown key was accepted")
	}
}
