package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
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
        "environment_namespace": "fixture",
		"models": {"route": {"connection": "provider", "provider_model": "model", "api": "responses", "responses": {"dialect": "lite"}, "exec_encoding": "freeform", "reasoning_efforts": ["high"]}},
		"connections": {"provider": {"base_url": "https://provider.invalid", "auth": {"type": "bearer", "credential": "fixture-secret"}}},
		"database_url": "postgresql://user:secret@db.invalid/sampler",
		"listen_tls": {"listen": "127.0.0.1:8083", "cert_file": "/fixture/tls.crt", "key_file": "/fixture/tls.key"},
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

func TestOwnershipCommandParsesMaintenanceModesBeforeOpeningDatabase(t *testing.T) {
	t.Setenv(samplerDatabaseURLEnv, "")
	tests := []struct {
		args []string
		want string
	}{
		{args: []string{"ownership", "unknown"}, want: `unknown ownership mode "unknown"`},
		{args: []string{"ownership", "inventory"}, want: "--account-uid is required"},
		{args: []string{"ownership", "forward", "--account-uid", "account-a"}, want: "--manifest is required for forward and reverse"},
		{args: []string{"ownership", "inventory", "--account-uid", "account-a", "--manifest", "/ids.json"}, want: "inventory does not accept --manifest"},
	}
	for _, test := range tests {
		t.Run(strings.Join(test.args, " "), func(t *testing.T) {
			var output bytes.Buffer
			handled, err := runOwnershipCommand(context.Background(), test.args, &output)
			if !handled {
				t.Fatal("maintenance command was not handled")
			}
			if err == nil || err.Error() != test.want {
				t.Fatalf("runOwnershipCommand error = %v, want %q", err, test.want)
			}
			if output.Len() != 0 {
				t.Fatalf("invalid maintenance command wrote stdout: %q", output.String())
			}
		})
	}
}

func TestCompactJournalRequiresScopeAndMode(t *testing.T) {
	t.Setenv(samplerDatabaseURLEnv, "")
	for _, args := range [][]string{
		{},
		{"--all"},
		{"--apply"},
		{"--all", "--session", "one", "--dry-run"},
		{"--session", "one", "--apply", "--dry-run"},
		{"--all", "--apply", "extra"},
	} {
		if err := runCompactJournal(t.Context(), args); err == nil || !strings.Contains(err.Error(), "select exactly one") {
			t.Errorf("compact-journal %v: got %v, want scope/mode error", args, err)
		}
	}
	for _, args := range [][]string{{"--session", "one", "--dry-run"}, {"--all", "--apply"}} {
		if err := runCompactJournal(t.Context(), args); err == nil || !strings.Contains(err.Error(), samplerDatabaseURLEnv) {
			t.Errorf("compact-journal %v: got %v, want missing database URL", args, err)
		}
	}
	if err := runCompactJournal(t.Context(), []string{
		"--session", "one", "--dry-run", "--event-block-level=5",
	}); err == nil || !strings.Contains(err.Error(), "invalid event block profile") {
		t.Fatalf("invalid compact-journal profile error=%v", err)
	}
}

func TestVerifyJournalRejectsArgumentsAndRequiresDatabaseURL(t *testing.T) {
	t.Setenv(samplerDatabaseURLEnv, "")
	if err := runVerifyJournal(t.Context(), []string{"--all"}); err == nil || err.Error() != "verify-journal does not accept arguments" {
		t.Fatalf("verify-journal argument error=%v", err)
	}
	if err := runVerifyJournal(t.Context(), nil); err == nil || !strings.Contains(err.Error(), samplerDatabaseURLEnv) {
		t.Fatalf("verify-journal missing database error=%v", err)
	}
}
