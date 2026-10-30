package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	databaseURLEnv = settingPrefix + "DATABASE_URL=postgres://test-user:never-print-this@test-database/notifications"
	// webhookSecretEnv and privateKeyEnv are the Secrets the deployment supplies as environment variables.
	webhookSecretEnv = settingPrefix + "GITHUB__WEBHOOK_SECRET=never-print-this-test-webhook-secret"
	privateKeyEnv    = settingPrefix + "GITHUB__PRIVATE_KEY=never-print-this-test-private-key"
)

// requiredFile is a settings file with every setting that has no default and is not a Secret.
const requiredFile = `namespace: test-agentplane
actions:
  url: http://test-actions.invalid:8080
  token_file: /test/tokens/actions
runner_gateway:
  control_target: "{sandbox}-control.{namespace}.svc:8080"
  token_file: /test/tokens/gateway
history_service:
  target: test-history.invalid:8080
  token_file: /test/tokens/history
session_dispatcher:
  target: test-dispatcher.invalid:8080
  token_file: /test/tokens/dispatcher
`

// deploymentFile is the settings file cluster/cdk8s/agentplane/notifications.py writes, in the keys it sets.
const deploymentFile = `namespace: test-agentplane
token_audience: test-notifications
operator_reader_account: test-app
github_delivery_retention_days: 14
notice_debounce:
  quiet_seconds: 60
  max_wait_seconds: 120
actions:
  url: http://test-actions.invalid:8080
  token_file: /test/tokens/actions
runner_gateway:
  control_target: "{sandbox}-control.{namespace}.svc:8080"
  token_file: /test/tokens/gateway
history_service:
  target: test-history.invalid:8080
  token_file: /test/tokens/history
session_dispatcher:
  target: test-dispatcher.invalid:8080
  token_file: /test/tokens/dispatcher
github:
  app_id: 1234
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

func TestTheSettingsTheDeploymentManifestsSetLoadWithTheirSecretsFromTheEnvironment(t *testing.T) {
	c, err := loadTestConfig(t, deploymentFile, webhookSecretEnv, privateKeyEnv)
	if err != nil {
		t.Fatal(err)
	}
	if c.DatabaseURL == "" || c.Namespace != "test-agentplane" || c.TokenAudience != "test-notifications" ||
		c.OperatorReaderAccount != "test-app" || (c.GitHubDeliveryRetentionDays == nil || *c.GitHubDeliveryRetentionDays != 14) || c.NoticeDebounce.QuietSeconds != 60 || c.NoticeDebounce.MaxWaitSeconds != 120 ||
		c.Actions.URL != "http://test-actions.invalid:8080" || c.Actions.TokenFile != "/test/tokens/actions" ||
		c.RunnerGateway.ControlTarget != "{sandbox}-control.{namespace}.svc:8080" || c.RunnerGateway.TokenFile != "/test/tokens/gateway" ||
		c.HistoryService.Target != "test-history.invalid:8080" || c.HistoryService.TokenFile != "/test/tokens/history" ||
		c.SessionDispatcher.Target != "test-dispatcher.invalid:8080" || c.SessionDispatcher.TokenFile != "/test/tokens/dispatcher" {
		t.Errorf("settings were not read: %+v", c)
	}
	if !c.githubEnabled() || c.GitHub.AppID != 1234 || c.GitHub.PrivateKey == "" || c.GitHub.WebhookSecret == "" {
		t.Errorf("the GitHub App was not read from the file and the environment: %+v", c.GitHub)
	}
}

func TestWhatADeploymentLeavesUnsetTakesItsDefault(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile)
	if err != nil {
		t.Fatal(err)
	}
	if c.Host != "0.0.0.0" || c.Port != 8080 || c.HTTPPort != 8081 || c.TokenAudience != "agentplane-notifications" || c.OperatorReaderAccount != "" {
		t.Errorf("listener or admission defaults: %+v", c)
	}
	if c.Quotas.InboxesPerAccount != 64 || c.Quotas.ActiveSubscriptionsPerInbox != 64 || c.Quotas.EntriesPerInbox != 10_000 {
		t.Errorf("quota defaults: %+v", c.Quotas)
	}
	if c.NoticeDebounce.QuietSeconds != 2 || c.NoticeDebounce.MaxWaitSeconds != 10 || c.StaleInboxConfirmationSeconds != 30 || (c.GitHubDeliveryRetentionDays == nil || *c.GitHubDeliveryRetentionDays != 7) {
		t.Errorf("worker defaults: %+v", c.Settings)
	}
	if c.RunnerGateway.RequestTimeoutSeconds != 5 || c.HistoryService.RequestTimeoutSeconds != 20 ||
		c.SessionDispatcher.RequestTimeoutSeconds != 15 || c.Actions.RequestTimeoutSeconds != 5 {
		t.Errorf("request timeout defaults: %+v", c)
	}
	if c.githubEnabled() {
		t.Errorf("GitHub is on without a setting: %+v", c.GitHub)
	}
}

func TestAnEnvironmentVariableOverridesTheFileAtTopLevelAndNested(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+"github_delivery_retention_days: 14\nport: 9000\nquotas:\n  entries_per_inbox: 5\n",
		settingPrefix+"PORT=9001", settingPrefix+"NOTICE_DEBOUNCE__QUIET_SECONDS=7", settingPrefix+"ACTIONS__REQUEST_TIMEOUT_S=1.5",
		settingPrefix+"STALE_INBOX_CONFIRMATION_S=9", settingPrefix+"GITHUB_DELIVERY_RETENTION_DAYS=3")
	if err != nil {
		t.Fatal(err)
	}
	if c.Port != 9001 || c.Quotas.EntriesPerInbox != 5 || c.NoticeDebounce.QuietSeconds != 7 ||
		c.Actions.RequestTimeoutSeconds != 1.5 || c.StaleInboxConfirmationSeconds != 9 || (c.GitHubDeliveryRetentionDays == nil || *c.GitHubDeliveryRetentionDays != 3) {
		t.Errorf("file and environment overrides: %+v", c)
	}
}

func TestActionsURLDerivesItsGRPCTargetAndTransport(t *testing.T) {
	for _, tc := range []struct {
		name      string
		url       string
		target    string
		tlsServer string
	}{
		{name: "HTTP with port", url: "http://actions.test:8080", target: "actions.test:8080"},
		{name: "HTTP normalizes scheme and port", url: "HTTP://actions.test:00080", target: "actions.test:80"},
		{name: "HTTP default port", url: "http://actions.test/", target: "actions.test:80"},
		{name: "HTTPS with port", url: "https://actions.test:8443", target: "actions.test:8443", tlsServer: "actions.test"},
		{name: "HTTPS default port", url: "https://actions.test", target: "actions.test:443", tlsServer: "actions.test"},
		{name: "IPv6", url: "http://[::1]:8080", target: "[::1]:8080"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			endpoint, err := (actionPeer{URL: tc.url}).grpcEndpoint()
			if err != nil {
				t.Fatal(err)
			}
			if endpoint.target != tc.target || endpoint.tlsServerName != tc.tlsServer {
				t.Errorf("endpoint = %+v, want target %q and TLS server %q", endpoint, tc.target, tc.tlsServer)
			}
		})
	}
}

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	for name, tc := range map[string]struct {
		file    string
		environ []string
	}{
		"unknown key":                 {file: requiredFile + "bogus: 1\n"},
		"unknown nested key":          {file: strings.Replace(requiredFile, "  target: test-history.invalid:8080\n", "  target: test-history.invalid:8080\n  bogus: 1\n", 1)},
		"no namespace":                {file: strings.Replace(requiredFile, "namespace: test-agentplane\n", "", 1)},
		"no Actions URL":              {file: strings.Replace(requiredFile, "  url: http://test-actions.invalid:8080\n", "", 1)},
		"Actions URL path":            {file: strings.Replace(requiredFile, "http://test-actions.invalid:8080", "http://test-actions.invalid:8080/v1", 1)},
		"Actions URL query":           {file: strings.Replace(requiredFile, "http://test-actions.invalid:8080", "http://test-actions.invalid:8080?", 1)},
		"Actions URL credentials":     {file: strings.Replace(requiredFile, "http://test-actions.invalid:8080", "http://test-user@actions.invalid:8080", 1)},
		"Actions URL invalid port":    {file: strings.Replace(requiredFile, "http://test-actions.invalid:8080", "http://test-actions.invalid:70000", 1)},
		"no Actions token file":       {file: strings.Replace(requiredFile, "  token_file: /test/tokens/actions\n", "", 1)},
		"no control target":           {file: strings.Replace(requiredFile, "  control_target: \"{sandbox}-control.{namespace}.svc:8080\"\n", "", 1)},
		"no History token file":       {file: strings.Replace(requiredFile, "  token_file: /test/tokens/history\n", "", 1)},
		"no Dispatcher target":        {file: strings.Replace(requiredFile, "  target: test-dispatcher.invalid:8080\n", "", 1)},
		"port out of range":           {file: requiredFile + "port: 70000\n"},
		"quota of none":               {file: requiredFile + "quotas:\n  inboxes_per_account: 0\n"},
		"debounce without a max wait": {file: requiredFile + "notice_debounce:\n  max_wait_seconds: 0\n"},
		"request timeout of none":     {file: requiredFile + "history_service:\n  request_timeout_s: 0\n"},
		"Action timeout of none":      {file: requiredFile + "actions:\n  request_timeout_s: 0\n"},
		"GitHub App without secrets":  {file: requiredFile + "github:\n  app_id: 1234\n"},
		"GitHub secrets without an App": {
			file: requiredFile, environ: []string{webhookSecretEnv, privateKeyEnv},
		},
		"short webhook secret": {
			file: requiredFile + "github:\n  app_id: 1234\n", environ: []string{settingPrefix + "GITHUB__WEBHOOK_SECRET=never-print-me", privateKeyEnv},
		},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := loadTestConfig(t, tc.file, tc.environ...)
			if err == nil {
				t.Fatal("accepted")
			}
			if strings.Contains(err.Error(), "never-print") {
				t.Errorf("the error carries a secret: %v", err)
			}
		})
	}
	if _, err := loadTestConfig(t, requiredFile, settingPrefix+"DATABASE_URL="); err == nil {
		t.Error("accepted without a database URL")
	}
}

func TestNullGitHubRetentionDisablesExpiry(t *testing.T) {
	for _, tc := range []struct {
		name, file, env string
		want            *int
	}{
		{"YAML null", requiredFile + "github_delivery_retention_days: null\n", "", nil},
		{"environment null", requiredFile + "github_delivery_retention_days: 14\n", settingPrefix + "GITHUB_DELIVERY_RETENTION_DAYS=null", nil},
		{"environment finite over YAML null", requiredFile + "github_delivery_retention_days: null\n", settingPrefix + "GITHUB_DELIVERY_RETENTION_DAYS=3", new(3)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			env := []string{}
			if tc.env != "" {
				env = append(env, tc.env)
			}
			c, err := loadTestConfig(t, tc.file, env...)
			if err != nil {
				t.Fatal(err)
			}
			got := c.GitHubDeliveryRetentionDays
			if (got == nil) != (tc.want == nil) || (got != nil && *got != *tc.want) {
				t.Fatalf("retention = %v, want %v", got, tc.want)
			}
		})
	}
}
