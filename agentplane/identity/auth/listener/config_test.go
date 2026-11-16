package listener

import (
	"reflect"
	"testing"
)

func TestAuthenticationDecodeHookKeepsVariantsClosed(t *testing.T) {
	target := reflect.TypeOf((*Authentication)(nil)).Elem()
	for name, data := range map[string]map[string]any{
		"unknown field":                  {"mode": "spiffe", "ca_file": "/roots.pem", "ignored": true},
		"irrelevant empty field":         {"mode": "spiffe", "ca_file": "/roots.pem", "audience": ""},
		"wrongly cased field":            {"mode": "spiffe", "CAFile": "/roots.pem"},
		"unknown mode":                   {"mode": "unknown"},
		"empty required field":           {"mode": "spiffe", "ca_file": ""},
		"wrong field type":               {"mode": "spiffe", "ca_file": 42},
		"missing required variant field": {"mode": "spiffe"},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := AuthenticationDecodeHook(reflect.TypeOf(data), target, data); err == nil {
				t.Fatal("invalid authentication accepted")
			}
		})
	}

	for name, data := range map[string]map[string]any{
		"SPIFFE": {"mode": "spiffe", "ca_file": "/roots.pem"},
	} {
		t.Run(name, func(t *testing.T) {
			decoded, err := AuthenticationDecodeHook(reflect.TypeOf(data), target, data)
			if err != nil {
				t.Fatal(err)
			}
			if _, ok := decoded.(Authentication); !ok {
				t.Fatalf("decoded value %T does not implement Authentication", decoded)
			}
		})
	}
}

func TestInvalidAuthenticationConfigurationIsRefused(t *testing.T) {
	for name, config := range map[string]Config{
		"empty":                 {},
		"no auth":               {ListenAddress: ":443"},
		"nil auth variant":      {ListenAddress: ":443", Auth: []Authentication{(*SPIFFEAuth)(nil)}},
		"duplicate":             {ListenAddress: ":443", Auth: []Authentication{&SPIFFEAuth{CAFile: "/roots.pem"}, &SPIFFEAuth{CAFile: "/roots.pem"}}},
		"spiffe without tls":    {ListenAddress: ":443", Auth: []Authentication{&SPIFFEAuth{CAFile: "/roots"}}},
		"spiffe relative roots": {ListenAddress: ":443", TLS: &TLS{CertFile: "/cert", KeyFile: "/key"}, Auth: []Authentication{&SPIFFEAuth{CAFile: "roots"}}},
		"relative serving key":  {ListenAddress: ":443", TLS: &TLS{CertFile: "/cert", KeyFile: "key"}, Auth: []Authentication{&SPIFFEAuth{CAFile: "/roots.pem"}}},
	} {
		t.Run(name, func(t *testing.T) {
			if err := config.Validate(); err == nil {
				t.Fatal("invalid configuration accepted")
			}
		})
	}
}
