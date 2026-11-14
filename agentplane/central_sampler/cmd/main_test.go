package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
)

func TestStartupDiagnosticsReportFieldAndOmitRawError(t *testing.T) {
	var output bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&output, nil))
	settings := validStartupDiagnosticSettings(t)
	invalidTimeout := 0
	settings.TurnTimeoutSeconds = &invalidTimeout
	err := atStartupStage("validate_settings", settings.Validate())
	logger.Error("central sampler stopped", startupLogAttrs(err)...)
	line := output.String()
	for _, want := range []string{"validate_settings", "turn_timeout_seconds", "must be positive and fit in time.Duration"} {
		if !strings.Contains(line, want) {
			t.Fatalf("diagnostic log lacks %q: %s", want, line)
		}
	}

	output.Reset()
	secret := "credential-sentinel"
	settings = validStartupDiagnosticSettings(t)
	connection := settings.Connections["provider"]
	connection.TLS = &peertls.ClientTLS{CAFile: "relative-path", ServerURI: "spiffe://secret.invalid/service"}
	connection.Auth.Credential = secret
	settings.Connections = map[string]samplersettings.ConnectionConfig{"connection-sentinel": connection}
	model := settings.Models["route"]
	model.Connection = "connection-sentinel"
	settings.Models = map[string]samplersettings.ModelConfig{"model-sentinel": model}
	validationErr := settings.Validate()
	if validationErr == nil {
		t.Fatal("accepted invalid provider TLS settings")
	}
	wrappedTLS := fmt.Errorf("credential=%s: %w", secret, validationErr)
	logger.Error("central sampler stopped", startupLogAttrs(atStartupStage("validate_settings", wrappedTLS))...)
	line = output.String()
	for _, hidden := range []string{secret, "model-sentinel", "connection-sentinel", "relative-path", "secret.invalid"} {
		if strings.Contains(line, hidden) {
			t.Fatalf("startup log disclosed wrapped TLS error detail %q: %s", hidden, line)
		}
	}
	if !strings.Contains(line, "validate_settings") || !strings.Contains(line, "connections.*.tls") {
		t.Fatalf("diagnostic log lacks stage: %s", line)
	}

	output.Reset()
	dsn := "postgresql://sampler:db-password@db.internal/sampler"
	logger.Error("central sampler stopped", startupLogAttrs(atStartupStage("database_migrations", errors.New(dsn)))...)
	line = output.String()
	if strings.Contains(line, dsn) || strings.Contains(line, "db-password") || strings.Contains(line, "db.internal") {
		t.Fatalf("startup log disclosed raw database error: %s", line)
	}
	if !strings.Contains(line, "database_migrations") {
		t.Fatalf("diagnostic log lacks stage: %s", line)
	}
}

func validStartupDiagnosticSettings(t *testing.T) samplersettings.Settings {
	t.Helper()
	settings, err := samplersettings.DecodeSettings(strings.NewReader(`{
		"models": {"route": {"connection": "provider", "provider_model": "model", "api": "responses", "responses": {"dialect": "lite"}, "exec_encoding": "freeform", "reasoning_efforts": ["high"]}},
		"connections": {"provider": {"base_url": "https://provider.invalid", "auth": {"type": "bearer", "credential": "fixture-secret"}}},
		"database_url": "postgresql://user:secret@db.invalid/sampler",
		"listen": "127.0.0.1:8080",
		"audience": "sampler",
		"allowed_accounts": ["namespace/service-account"],
		"code_mode_host": "/usr/local/bin/codex-code-mode-host",
		"executor_codex": "/usr/local/bin/codex",
		"logs": "/tmp/sampler-logs",
		"gateway_target": "runner-gateway.{namespace}.svc:8080",
		"gateway_token_file": "/var/run/secrets/gateway/token",
		"max_live_sessions": 1,
		"rpc_timeout_seconds": 1,
		"lease_seconds": 60
	}`))
	if err != nil {
		t.Fatalf("decode valid diagnostic settings: %v", err)
	}
	return settings
}

func TestStartupFailurePreservesCancellation(t *testing.T) {
	if !errors.Is(atStartupStage("service_runtime", context.Canceled), context.Canceled) {
		t.Fatal("startup stage wrapper hid cancellation")
	}
}
