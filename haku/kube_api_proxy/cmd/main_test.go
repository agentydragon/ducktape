package main

import (
	"testing"
	"time"

	"github.com/caarlos0/env/v11"
)

func TestParseEnvironmentConfig(t *testing.T) {
	config, err := parseEnvironmentConfig(env.Options{Environment: map[string]string{
		"HAKU_KUBE_AUTHORIZATION_URL":            "https://console.test/api/internal/kubernetes/authorize",
		"HAKU_KUBE_STREAM_REVALIDATION_INTERVAL": "7s",
		"KUBERNETES_SERVICE_HOST":                "10.0.0.1",
		"KUBERNETES_SERVICE_PORT_HTTPS":          "6443",
	}})
	if err != nil {
		t.Fatal(err)
	}
	if config.AuthorizationURL.String() != "https://console.test/api/internal/kubernetes/authorize" {
		t.Fatalf("authorization URL = %q", config.AuthorizationURL.String())
	}
	if config.ListenAddress != ":8080" || config.AuthorizationTimeout != 3*time.Second || config.RequestTimeout != 30*time.Second || config.MaxRequestBytes != 10<<20 {
		t.Fatalf("unexpected defaults: %#v", config)
	}
	if config.StreamRevalidationInterval != 7*time.Second {
		t.Fatalf("stream revalidation interval = %s", config.StreamRevalidationInterval)
	}
	if config.kubernetesServicePort() != "6443" {
		t.Fatalf("Kubernetes service port = %q", config.kubernetesServicePort())
	}
	if config.TLSListenAddress != ":8443" || config.TLSCertFile != "" || config.TLSKeyFile != "" {
		t.Fatalf("unexpected TLS defaults: %#v", config)
	}
}

func TestParseEnvironmentConfigAcceptsTLSPair(t *testing.T) {
	config, err := parseEnvironmentConfig(env.Options{Environment: map[string]string{
		"HAKU_KUBE_AUTHORIZATION_URL":   "https://console.test/api/internal/kubernetes/authorize",
		"KUBERNETES_SERVICE_HOST":       "10.0.0.1",
		"KUBERNETES_SERVICE_PORT_HTTPS": "6443",
		"HAKU_KUBE_TLS_CERT_FILE":       "/tls/tls.crt",
		"HAKU_KUBE_TLS_KEY_FILE":        "/tls/tls.key",
	}})
	if err != nil {
		t.Fatal(err)
	}
	if config.TLSCertFile != "/tls/tls.crt" || config.TLSKeyFile != "/tls/tls.key" {
		t.Fatalf("unexpected TLS files: %#v", config)
	}
}

func TestParseEnvironmentConfigRejectsInvalidValues(t *testing.T) {
	base := map[string]string{
		"HAKU_KUBE_AUTHORIZATION_URL": "https://console.test/api/internal/kubernetes/authorize",
		"KUBERNETES_SERVICE_HOST":     "10.0.0.1",
		"KUBERNETES_SERVICE_PORT":     "443",
	}
	for name, value := range map[string]string{
		"HAKU_KUBE_AUTHORIZATION_URL":            "/relative/authorize",
		"HAKU_KUBE_AUTHORIZATION_TIMEOUT":        "0s",
		"HAKU_KUBE_REQUEST_TIMEOUT":              "not-a-duration",
		"HAKU_KUBE_STREAM_REVALIDATION_INTERVAL": "0s",
		"HAKU_KUBE_MAX_REQUEST_BYTES":            "0",
		// A lone half of the TLS pair is a broken deploy, not a plaintext-only one.
		"HAKU_KUBE_TLS_CERT_FILE": "/tls/tls.crt",
		"HAKU_KUBE_TLS_KEY_FILE":  "/tls/tls.key",
	} {
		t.Run(name, func(t *testing.T) {
			environment := make(map[string]string, len(base)+1)
			for key, baseValue := range base {
				environment[key] = baseValue
			}
			environment[name] = value
			if _, err := parseEnvironmentConfig(env.Options{Environment: environment}); err == nil {
				t.Fatalf("%s=%q was accepted", name, value)
			}
		})
	}
}
