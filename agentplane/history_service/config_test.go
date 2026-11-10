package main

import (
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
)

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgres://test-user:never-print-this@test-database/history"

// requiredFile is a deployment's settings file with every setting that has no default.
const requiredFile = `rpc:
  listen_address: 0.0.0.0:8080
  auth:
    - mode: kubernetes_bearer
      audiences: [test-history-service, test-runner-ingress]
reader_accounts:
  - {namespace: test-agentplane, name: test-app}
  - test-agentplane/test-notifications
token_audience: test-history-service
ingress:
  token_audience: test-runner-ingress
  writer_namespaces: [test-agentplane]
`

func loadTestConfig(t *testing.T, file string, environ ...string) (config, error) {
	t.Helper()
	return configtest.Load(t, settingPrefix, loadConfig, append([]string{
		settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, file),
		databaseURLEnv,
	}, environ...))
}

func TestSettingsLoadFromTheFileAndTheEnvironmentWithTheirDefaults(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile)
	if err != nil {
		t.Fatal(err)
	}
	wantReaders := []auth.Account{{Namespace: "test-agentplane", Name: "test-app"}, {Namespace: "test-agentplane", Name: "test-notifications"}}
	if !slices.Equal(c.ReaderAccounts, wantReaders) || c.DatabaseURL == "" || c.Ingress.WriterNamespaces[0] != "test-agentplane" {
		t.Errorf("settings were not read: %+v", c)
	}
	if !c.Ingress.SettleDeltas {
		t.Error("default ingress does not settle deltas")
	}
}

func TestDelegationTrustIsAnIndependentExactMethodSetting(t *testing.T) {
	file := requiredFile + `delegation:
  actor: test-agentplane/actions
  caller_namespaces: [test-agentplane]
  methods: [/agentplane.history_service.HistoryService/ReadSessionEvents]
`
	c, err := loadTestConfig(t, file)
	if err != nil {
		t.Fatal(err)
	}
	if c.Delegation == nil || c.Delegation.Actor != (auth.Account{Namespace: "test-agentplane", Name: "actions"}) ||
		!slices.Equal(c.Delegation.CallerNamespaces, []string{"test-agentplane"}) ||
		!slices.Equal(c.Delegation.Methods, []string{"/agentplane.history_service.HistoryService/ReadSessionEvents"}) {
		t.Fatalf("delegation settings were not kept separate and explicit: %+v", c.Delegation)
	}
	for _, incomplete := range []string{
		requiredFile + "delegation: {actor: test-agentplane/actions}\n",
		requiredFile + "delegation: {actor: test-agentplane/actions, caller_namespaces: [], methods: [x]}\n",
		requiredFile + "delegation: {actor: malformed, caller_namespaces: [test-agentplane], methods: [x]}\n",
	} {
		if _, err := loadTestConfig(t, incomplete); err == nil {
			t.Fatalf("accepted incomplete or malformed delegation setting: %s", incomplete)
		}
	}
}

func TestASettingAFileSetsToItsNonDefaultOverridesTheDefault(t *testing.T) {
	c, err := loadTestConfig(t, strings.Replace(requiredFile, "0.0.0.0:8080", "127.0.0.1:9000", 1)+"watch_lease_s: 1.5\npolicy_namespace: test-policies\n", settingPrefix+"FOLD_CONCURRENCY=7")
	if err != nil {
		t.Fatal(err)
	}
	if c.RPC.ListenAddress != "127.0.0.1:9000" || c.WatchLeaseSeconds != 1.5 || c.FoldConcurrency != 7 || c.PolicyNamespace != "test-policies" {
		t.Errorf("file and environment overrides: %+v", c)
	}
	c, err = loadTestConfig(t, strings.Replace(requiredFile, "  writer_namespaces", "  settle_deltas: false\n  writer_namespaces", 1))
	if err != nil {
		t.Fatal(err)
	}
	if c.Ingress.SettleDeltas {
		t.Error("settle_deltas: false was not applied")
	}
}

func TestNativeFrameReadBatchSizeLoadsFromFileAndEnvironment(t *testing.T) {
	fileSetting := requiredFile + "native_frame_read_batch_size: 17\n"
	c, err := loadTestConfig(t, fileSetting)
	if err != nil {
		t.Fatal(err)
	}
	if c.NativeFrameReadBatchSize != 17 {
		t.Fatalf("file setting = %d, want 17", c.NativeFrameReadBatchSize)
	}

	c, err = loadTestConfig(t, fileSetting, settingPrefix+"NATIVE_FRAME_READ_BATCH_SIZE=3")
	if err != nil {
		t.Fatal(err)
	}
	if c.NativeFrameReadBatchSize != 3 {
		t.Fatalf("environment setting = %d, want 3", c.NativeFrameReadBatchSize)
	}
}

func TestMigrationTimeoutSettings(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+"migration_timeout_s: 45\n", settingPrefix+"MIGRATION_TIMEOUT_S=90")
	if err != nil || c.MigrationTimeoutSeconds != 90 {
		t.Fatalf("environment override: timeout %v, error %v", c.MigrationTimeoutSeconds, err)
	}
	timeout, err := migrationOnlyTimeout()
	if err != nil || timeout != 90*time.Second {
		t.Fatalf("migration-only timeout %v, error %v", timeout, err)
	}
	for _, value := range []string{"0", "NaN", "86401", "invalid"} {
		t.Setenv(settingPrefix+"MIGRATION_TIMEOUT_S", value)
		if _, err := migrationOnlyTimeout(); err == nil {
			t.Errorf("migration-only accepted timeout %q", value)
		}
	}
}

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	for name, file := range map[string]string{
		"unknown key":                      requiredFile + "bogus: 1\n",
		"no readers":                       "reader_accounts: []\n" + requiredFile[strings.Index(requiredFile, "token_audience"):],
		"no audience":                      strings.Replace(requiredFile, "token_audience: test-history-service\n", "", 1),
		"no ingress audience":              strings.Replace(requiredFile, "  token_audience: test-runner-ingress\n", "", 1),
		"no writer namespaces":             strings.Replace(requiredFile, "  writer_namespaces: [test-agentplane]\n", "", 1),
		"request timeout too long":         requiredFile + "request_timeout_s: 61\n",
		"fold page larger than served":     requiredFile + "fold_page_size: 1001\n",
		"fold without concurrency":         requiredFile + "fold_concurrency: 0\n",
		"zero native frame read batch":     requiredFile + "native_frame_read_batch_size: 0\n",
		"negative native frame read batch": requiredFile + "native_frame_read_batch_size: -1\n",
	} {
		t.Run(name, func(t *testing.T) {
			_, err := loadTestConfig(t, file)
			if err == nil {
				t.Fatal("accepted")
			}
			if strings.Contains(err.Error(), "never-print-this") {
				t.Errorf("the error carries the database URL: %v", err)
			}
		})
	}
	if _, err := loadTestConfig(t, requiredFile, databaseURLEnv[:len(settingPrefix)]+"DATABASE_URL="); err == nil {
		t.Error("accepted without a database URL")
	}
}

func TestSecondsConvertToADuration(t *testing.T) {
	if got := seconds(0.2); got != 200*time.Millisecond {
		t.Errorf("seconds(0.2) = %v, want 200ms", got)
	}
}
