package main

import (
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
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
policy_namespace: test-policies
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

func TestSettingsLoadFromTheFileAndRuntimeSecretWithTheirDefaults(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile)
	if err != nil {
		t.Fatal(err)
	}
	wantReaders := []auth.Account{{Namespace: "test-agentplane", Name: "test-app"}, {Namespace: "test-agentplane", Name: "test-notifications"}}
	if !slices.Equal(c.ReaderAccounts, wantReaders) || c.DatabaseURL != "postgres://test-user:never-print-this@test-database/history" ||
		c.Ingress.WriterNamespaces[0] != "test-agentplane" {
		t.Errorf("settings were not read: %+v", c)
	}
	if !c.Ingress.SettleDeltas {
		t.Error("default ingress does not settle deltas")
	}
	if c.Ingress.CompactDeltas {
		t.Error("compaction must be explicitly enabled")
	}
}

func TestDelegationTrustIsAnIndependentExactMethodSetting(t *testing.T) {
	file := requiredFile + `delegation:
  actor: test-agentplane/actions
  identity_callers: true
  methods: [/agentplane.history_service.HistoryService/ReadSessionEvents]
`
	c, err := loadTestConfig(t, file)
	if err != nil {
		t.Fatal(err)
	}
	if c.Delegation == nil || c.Delegation.Actor != (auth.Account{Namespace: "test-agentplane", Name: "actions"}) ||
		!slices.Equal(c.Delegation.Methods, []string{"/agentplane.history_service.HistoryService/ReadSessionEvents"}) {
		t.Fatalf("delegation settings were not kept separate and explicit: %+v", c.Delegation)
	}
	for _, incomplete := range []string{
		requiredFile + "delegation: {actor: test-agentplane/actions}\n",
		requiredFile + "delegation: {actor: test-agentplane/actions, identity_callers: false, methods: [x]}\n",
		requiredFile + "delegation: {actor: malformed, identity_callers: true, methods: [x]}\n",
		requiredFile + "delegation: {actor: test-agentplane/actions, identity_callers: true, caller_namespaces: [test-agentplane], methods: [x]}\n",
	} {
		if _, err := loadTestConfig(t, incomplete); err == nil {
			t.Fatalf("accepted incomplete or malformed delegation setting: %s", incomplete)
		}
	}
	identityOnly, err := loadTestConfig(t, requiredFile+"delegation: {actor: test-agentplane/actions, identity_callers: true, methods: [x]}\n")
	if err != nil || !identityOnly.Delegation.IdentityCallers {
		t.Fatalf("an Identity-only delegation grant: %+v %v", identityOnly.Delegation, err)
	}
}

// The sampler's grant to push as a delegating actor for a Session's Identity is its own setting,
// and needs what Push admitting Identity writers needs: the policy namespace and the SPIFFE listener.
func TestSamplerIdentityCallersNeedsSamplerAccountsThePolicyNamespaceAndTheSPIFFEListener(t *testing.T) {
	const granted = `rpc:
  listen_address: 0.0.0.0:8080
  tls: {cert_file: /test/tls.crt, key_file: /test/tls.key}
  auth:
    - mode: kubernetes_bearer
      audiences: [test-history-service, test-runner-ingress]
    - mode: spiffe
      ca_file: /test/workload-ca.crt
reader_accounts:
  - test-agentplane/test-app
token_audience: test-history-service
policy_namespace: test-policies
ingress:
  token_audience: test-runner-ingress
  writer_namespaces: [test-agentplane]
  sampler_accounts: [test-agentplane/test-sampler]
  sampler_identity_callers: true
`
	c, err := loadTestConfig(t, granted)
	if err != nil || !c.Ingress.SamplerIdentityCallers || len(c.Ingress.SamplerAccounts) != 1 {
		t.Fatalf("sampler delegation grant: %+v %v", c.Ingress, err)
	}
	for name, incomplete := range map[string]string{
		"no policy namespace": strings.Replace(granted, "policy_namespace: test-policies\n", "", 1),
		"no sampler accounts": strings.Replace(granted, "  sampler_accounts: [test-agentplane/test-sampler]\n", "", 1),
		"a bearer-only listener": strings.Replace(strings.Replace(granted, "  tls: {cert_file: /test/tls.crt, key_file: /test/tls.key}\n", "", 1),
			"    - mode: spiffe\n      ca_file: /test/workload-ca.crt\n", "", 1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadTestConfig(t, incomplete); err == nil {
				t.Fatalf("accepted the sampler delegation grant without its prerequisites:\n%s", incomplete)
			}
		})
	}
}

func TestSettingsFileValuesIgnoreGenericEnvironmentOverrides(t *testing.T) {
	file := strings.Replace(requiredFile, "0.0.0.0:8080", "127.0.0.1:9000", 1) + "watch_lease_s: 1.5\nfold_concurrency: 7\n"
	c, err := loadTestConfig(t, file, settingPrefix+"FOLD_CONCURRENCY=9", settingPrefix+"RPC__LISTEN_ADDRESS=127.0.0.1:9999")
	if err != nil {
		t.Fatal(err)
	}
	if c.RPC.ListenAddress != "127.0.0.1:9000" || c.WatchLeaseSeconds != 1.5 || c.FoldConcurrency != 7 || c.PolicyNamespace != "test-policies" {
		t.Errorf("settings file values changed under environment overrides: %+v", c)
	}
	c, err = loadTestConfig(t, strings.Replace(requiredFile, "  writer_namespaces", "  settle_deltas: false\n  writer_namespaces", 1))
	if err != nil {
		t.Fatal(err)
	}
	if c.Ingress.SettleDeltas {
		t.Error("settle_deltas: false was not applied")
	}
	c, err = loadTestConfig(t, strings.Replace(requiredFile, "  writer_namespaces", "  compact_deltas: true\n  writer_namespaces", 1))
	if err != nil || !c.Ingress.CompactDeltas {
		t.Fatalf("compact_deltas: true was not applied: %v", err)
	}
}

func TestNativeFrameReadBatchSizeLoadsFromFileAndIgnoresEnvironment(t *testing.T) {
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
	if c.NativeFrameReadBatchSize != 17 {
		t.Fatalf("environment changed file setting to %d, want 17", c.NativeFrameReadBatchSize)
	}
}

func TestMigrationTimeoutIsReadFromTheSettingsFile(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+"migration_timeout_s: 45\n", settingPrefix+"MIGRATION_TIMEOUT_S=90")
	if err != nil || c.MigrationTimeoutSeconds != 45 {
		t.Fatalf("settings file timeout %v, error %v", c.MigrationTimeoutSeconds, err)
	}
	for _, value := range []string{"0", "NaN", "86401", "invalid"} {
		if _, err := loadTestConfig(t, requiredFile+"migration_timeout_s: "+value+"\n"); err == nil {
			t.Errorf("settings file accepted migration timeout %q", value)
		}
	}
}

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	for name, file := range map[string]string{
		"unknown key":                      requiredFile + "bogus: 1\n",
		"runtime secret in file":           requiredFile + "database_url: postgres://file.invalid/history\n",
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

func TestSettingsFilePathIsRequiredAndMustBeReadable(t *testing.T) {
	if _, err := configtest.Load(t, settingPrefix, loadConfig, []string{databaseURLEnv}); err == nil {
		t.Fatal("started without a settings file path")
	}
	if _, err := configtest.Load(t, settingPrefix, loadConfig, []string{
		settingPrefix + "CONFIG_FILE=/settings/file/does/not/exist", databaseURLEnv,
	}); err == nil {
		t.Fatal("started without the required settings file")
	}
	if _, err := loadTestConfig(t, requiredFile+"---\n"); err == nil {
		t.Fatal("accepted more than one YAML document")
	}
}

func TestSecondsConvertToADuration(t *testing.T) {
	if got := seconds(0.2); got != 200*time.Millisecond {
		t.Errorf("seconds(0.2) = %v, want 200ms", got)
	}
}
