package runner

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestGuestConfigurationBuildsIsolatedHarnessSettings(t *testing.T) {
	preserveGuestEnvironment(t)
	configPath, launchPath := writeGuestFixture(
		t,
		map[string]any{
			"version":               1,
			"control":               map[string]string{"target": "control.test:7100", "bearer": "control-placeholder"},
			"ingress":               map[string]string{"target": "ingress.test:8081", "bearer": "ingress-placeholder"},
			"llm_base_url":          "http://llm.test:8080/",
			"proxy_url":             "http://proxy.test:3128/",
			"platform_instructions": "Test platform guidance.",
			"console_autologin":     true,
		},
		map[string]any{
			"sandbox":   map[string]string{"namespace": "test", "name": "sandbox", "uid": "sandbox-uid"},
			"bootstrap": "echo initialized",
		},
	)

	config, control, ingress, err := GuestConfiguration(configPath, launchPath)
	if err != nil {
		t.Fatal(err)
	}
	if control != (Endpoint{Target: "control.test:7100", Bearer: "control-placeholder"}) {
		t.Fatalf("control endpoint = %+v", control)
	}
	if ingress != (Endpoint{Target: "ingress.test:8081", Bearer: "ingress-placeholder"}) {
		t.Fatalf("ingress endpoint = %+v", ingress)
	}
	if config.StateDir != guestStateDir || config.WorkspaceRoot != guestWorkspaceDir || config.InitializationCwd != guestWorkspaceDir {
		t.Fatalf("guest paths = state %q, workspace %q, initialization %q", config.StateDir, config.WorkspaceRoot, config.InitializationCwd)
	}
	if config.Launch.Sandbox != (SandboxIdentity{Namespace: "test", Name: "sandbox", UID: "sandbox-uid"}) || config.Launch.Bootstrap == nil || *config.Launch.Bootstrap != "echo initialized" || config.Launch.PlatformInstructions != "Test platform guidance." {
		t.Fatalf("guest launch config = %+v", config.Launch)
	}
	if config.Claude == nil || config.Claude.BaseURL != "http://llm.test:8080" || config.Claude.Token != workloadToken {
		t.Fatalf("Claude launch config = %+v", config.Claude)
	}
	if config.Codex == nil || config.Codex.BaseURL != "http://llm.test:8080/v1" || config.Codex.Token != workloadToken {
		t.Fatalf("Codex launch config = %+v", config.Codex)
	}
	if config.ProcessIsolation == nil || config.ProcessIsolation.AgentUID != guestAgentUID || config.ProcessIsolation.AgentGID != guestAgentGID || !reflect.DeepEqual(config.ProcessIsolation.BlockDevices, []string{guestWorkspaceDev}) {
		t.Fatalf("guest process isolation = %+v", config.ProcessIsolation)
	}
	for key, want := range map[string]string{
		"HTTP_PROXY":    "http://proxy.test:3128",
		"HTTPS_PROXY":   "http://proxy.test:3128",
		"SSL_CERT_FILE": guestCABundle,
		"NO_PROXY":      "127.0.0.1,localhost,control.test,ingress.test",
	} {
		if got := os.Getenv(key); got != want {
			t.Errorf("runner environment %s = %q, want %q", key, got, want)
		}
	}
	if config.HarnessEnvironment["KUBECONFIG"] != guestKubeconfig {
		t.Fatalf("guest harness KUBECONFIG = %q", config.HarnessEnvironment["KUBECONFIG"])
	}
	if _, exists := config.HarnessEnvironment["ANTHROPIC_AUTH_TOKEN"]; exists {
		t.Error("guest runner configuration leaked the real Anthropic token into harness environment")
	}
	if _, exists := config.HarnessEnvironment["OPENAI_API_KEY"]; exists {
		t.Error("guest runner configuration leaked the real OpenAI key into harness environment")
	}
}

func TestGuestConfigurationRejectsInvalidSeedAndLaunch(t *testing.T) {
	tests := []struct {
		name   string
		seed   map[string]any
		launch map[string]any
	}{
		{name: "unsupported version", seed: map[string]any{"version": 2}},
		{name: "missing endpoint bearer", seed: map[string]any{"control": map[string]string{"target": "control.test:7100"}}},
		{name: "empty endpoint", seed: map[string]any{"ingress": map[string]string{"target": "", "bearer": "token"}}},
		{name: "non-http origin", seed: map[string]any{"llm_base_url": "ftp://llm.test"}},
		{name: "hostless origin", seed: map[string]any{"llm_base_url": "llm.test"}},
		{name: "origin path", seed: map[string]any{"llm_base_url": "https://llm.test/v1"}},
		{name: "origin credentials", seed: map[string]any{"llm_base_url": "https://user:secret@llm.test"}},
		{name: "invalid port", seed: map[string]any{"proxy_url": "http://proxy.test:65536"}},
		{name: "zero port", seed: map[string]any{"proxy_url": "http://proxy.test:0"}},
		{name: "empty platform instructions", seed: map[string]any{"platform_instructions": ""}},
		{name: "unknown seed field", seed: map[string]any{"unexpected": "value"}},
		{name: "missing Sandbox identity", launch: map[string]any{"sandbox": map[string]string{"namespace": "test", "name": "sandbox"}}},
		{name: "empty bootstrap", launch: map[string]any{"bootstrap": ""}},
		{name: "unknown launch field", launch: map[string]any{"unexpected": "value"}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			preserveGuestEnvironment(t)
			seed := validGuestSeed()
			mergeGuestValues(seed, test.seed)
			launch := validGuestLaunch()
			mergeGuestValues(launch, test.launch)
			configPath, launchPath := writeGuestFixture(t, seed, launch)
			if _, _, _, err := GuestConfiguration(configPath, launchPath); err == nil {
				t.Fatal("GuestConfiguration accepted invalid input")
			}
		})
	}
}

func writeGuestFixture(t *testing.T, seed, launch any) (string, string) {
	t.Helper()
	directory := t.TempDir()
	configPath := filepath.Join(directory, "config.json")
	launchPath := filepath.Join(directory, "launch.json")
	writeJSONFixture(t, configPath, seed)
	writeJSONFixture(t, launchPath, launch)
	return configPath, launchPath
}

func writeJSONFixture(t *testing.T, path string, value any) {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func validGuestSeed() map[string]any {
	return map[string]any{
		"version":               1,
		"control":               map[string]string{"target": "control.test:7100", "bearer": "control-placeholder"},
		"ingress":               map[string]string{"target": "ingress.test:8081", "bearer": "ingress-placeholder"},
		"llm_base_url":          "http://llm.test:8080",
		"proxy_url":             "http://proxy.test:3128",
		"platform_instructions": "Test platform guidance.",
	}
}

func validGuestLaunch() map[string]any {
	return map[string]any{"sandbox": map[string]string{"namespace": "test", "name": "sandbox", "uid": "sandbox-uid"}}
}

func mergeGuestValues(target, updates map[string]any) {
	for key, value := range updates {
		target[key] = value
	}
}

func preserveGuestEnvironment(t *testing.T) {
	t.Helper()
	keys := []string{"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE"}
	values := make(map[string]string, len(keys))
	present := make(map[string]bool, len(keys))
	for _, key := range keys {
		values[key], present[key] = os.LookupEnv(key)
	}
	t.Cleanup(func() {
		for _, key := range keys {
			if present[key] {
				_ = os.Setenv(key, values[key])
			} else {
				_ = os.Unsetenv(key)
			}
		}
	})
}
