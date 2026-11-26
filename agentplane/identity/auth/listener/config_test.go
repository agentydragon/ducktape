package listener

import (
	"fmt"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/configfile"
)

func TestDecodeListenerAuthentication(t *testing.T) {
	const valid = "{mode: spiffe, ca_file: /roots.pem}"
	for _, enclosing := range []struct {
		key     string
		options configfile.MapstructureOptions
	}{
		{key: "rpc", options: configfile.MapstructureOptions{ExactFieldNames: true}},
		{key: "RPC", options: configfile.MapstructureOptions{WeaklyTypedInput: true}},
	} {
		for name, test := range map[string]struct {
			auth, errorText string
		}{
			"valid":               {auth: "[" + valid + "]"},
			"unknown field":       {auth: "[{mode: spiffe, ca_file: /roots.pem, ignored: true}]", errorText: "invalid keys"},
			"retired field":       {auth: "[{mode: spiffe, ca_file: /roots.pem, audience: ''}]", errorText: "invalid keys"},
			"wrong mode key":      {auth: "[{Mode: spiffe, ca_file: /roots.pem}]", errorText: "authentication mode is required"},
			"wrong case":          {auth: "[{mode: spiffe, CA_FILE: /roots.pem}]", errorText: "invalid keys"},
			"unknown mode":        {auth: "[{mode: kubernetes_bearer}]", errorText: "unknown authentication mode"},
			"missing mode":        {auth: "[{ca_file: /roots.pem}]", errorText: "authentication mode is required"},
			"non-string mode":     {auth: "[{mode: true, ca_file: /roots.pem}]", errorText: "authentication mode must be a string"},
			"empty trust roots":   {auth: "[{mode: spiffe, ca_file: ''}]", errorText: "must be a non-empty string"},
			"missing trust roots": {auth: "[{mode: spiffe}]", errorText: "must be a non-empty string"},
			"non-string roots":    {auth: "[{mode: spiffe, ca_file: 42}]", errorText: "ca_file"},
		} {
			t.Run(enclosing.key+"/"+name, func(t *testing.T) {
				var cfg struct {
					RPC Config `mapstructure:"rpc"`
				}
				yaml := fmt.Sprintf("%s:\n  listen_address: :443\n  tls: {cert_file: /cert, key_file: /key}\n  auth: %s\n",
					enclosing.key, test.auth)
				_, err := configfile.DecodeMapstructureYAML([]byte(yaml), &cfg, enclosing.options)
				if test.errorText != "" {
					if err == nil || !strings.Contains(err.Error(), test.errorText) {
						t.Fatalf("error = %v, want %q", err, test.errorText)
					}
					return
				}
				if err != nil {
					t.Fatal(err)
				}
				if err := cfg.RPC.Validate(); err != nil {
					t.Fatal(err)
				}
				if got := cfg.RPC.Auth[0].CAFile; got != "/roots.pem" {
					t.Fatalf("ca_file = %q, want /roots.pem", got)
				}
			})
		}
	}
}

func TestNullAuthenticationEntryFailsValidation(t *testing.T) {
	const yaml = "listen_address: :443\ntls: {cert_file: /cert, key_file: /key}\nauth: [null]\n"
	var cfg Config
	if _, err := configfile.DecodeMapstructureYAML([]byte(yaml), &cfg, configfile.MapstructureOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := cfg.Validate(); err == nil || !strings.Contains(err.Error(), "authentication mode is required") {
		t.Fatalf("auth: [null] validation error = %v", err)
	}
}

func TestInvalidAuthenticationConfigurationIsRefused(t *testing.T) {
	for name, config := range map[string]Config{
		"empty":                 {},
		"no auth":               {ListenAddress: ":443"},
		"nil auth":              {ListenAddress: ":443", Auth: []*SPIFFEAuth{nil}},
		"duplicate":             {ListenAddress: ":443", Auth: []*SPIFFEAuth{{CAFile: "/roots.pem"}, {CAFile: "/roots.pem"}}},
		"spiffe without tls":    {ListenAddress: ":443", Auth: []*SPIFFEAuth{{CAFile: "/roots"}}},
		"spiffe relative roots": {ListenAddress: ":443", TLS: &TLS{CertFile: "/cert", KeyFile: "/key"}, Auth: []*SPIFFEAuth{{CAFile: "roots"}}},
		"relative serving key":  {ListenAddress: ":443", TLS: &TLS{CertFile: "/cert", KeyFile: "key"}, Auth: []*SPIFFEAuth{{CAFile: "/roots.pem"}}},
	} {
		t.Run(name, func(t *testing.T) {
			if cfg, err := config.TLSConfig("spiffe://agentplane/test"); err == nil || cfg != nil {
				t.Fatalf("invalid configuration allowed TLS setup: config = %v, error = %v", cfg, err)
			}
		})
	}
}
