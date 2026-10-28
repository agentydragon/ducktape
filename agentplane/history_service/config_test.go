package main

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgres://test-user:never-print-this@test-database/history"

// requiredFile is a deployment's settings file with every setting that has no default.
const requiredFile = `reader_accounts:
  - {namespace: test-agentplane, name: test-app}
  - test-agentplane/test-notifications
token_audience: test-history-service
ingress:
  token_audience: test-runner-ingress
  writer_namespaces: [test-agentplane]
`

func loadTestConfig(t *testing.T, file string, environ ...string) (config, error) {
	t.Helper()
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(name, settingPrefix) {
			t.Setenv(name, "")
		}
	}
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(file), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, entry := range append([]string{settingPrefix + "CONFIG_FILE=" + path, databaseURLEnv}, environ...) {
		name, value, _ := strings.Cut(entry, "=")
		t.Setenv(name, value)
	}
	return loadConfig()
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
	// What a deployment leaves unset: the listeners remain on their exposed endpoints, and the ingress settles deltas.
	if c.ListenAddress != "0.0.0.0:8080" || c.Ingress.ListenAddress != "0.0.0.0:8081" || !c.Ingress.SettleDeltas {
		t.Errorf("defaults: reader %q, ingress %q, settle deltas %v", c.ListenAddress, c.Ingress.ListenAddress, c.Ingress.SettleDeltas)
	}
}

func TestASettingAFileSetsToItsNonDefaultOverridesTheDefault(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+"listen_address: 127.0.0.1:9000\nwatch_lease_s: 1.5\n", settingPrefix+"FOLD_CONCURRENCY=7")
	if err != nil {
		t.Fatal(err)
	}
	if c.ListenAddress != "127.0.0.1:9000" || c.WatchLeaseSeconds != 1.5 || c.FoldConcurrency != 7 {
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

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	for name, file := range map[string]string{
		"unknown key":                      requiredFile + "bogus: 1\n",
		"no readers":                       "reader_accounts: []\n" + requiredFile[strings.Index(requiredFile, "token_audience"):],
		"no audience":                      strings.Replace(requiredFile, "token_audience: test-history-service\n", "", 1),
		"no ingress audience":              strings.Replace(requiredFile, "  token_audience: test-runner-ingress\n", "", 1),
		"no writer namespaces":             strings.Replace(requiredFile, "  writer_namespaces: [test-agentplane]\n", "", 1),
		"removed listener settings":        requiredFile + "host: 0.0.0.0\nport: 8080\n",
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
