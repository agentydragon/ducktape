package runner

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func configurationFixture(t *testing.T) (map[string]any, string, string) {
	t.Helper()
	directory := t.TempDir()
	configPath, launchPath := filepath.Join(directory, "runner.json"), filepath.Join(directory, "launch.json")
	writeJSONFixture(t, launchPath, map[string]any{"sandbox": map[string]string{"namespace": "test", "name": "sandbox", "uid": "sandbox-uid"}, "bootstrap": "echo initialized"})
	value := map[string]any{
		"environment": map[string]any{"kind": "pod"},
		"version":     2, "state_dir": filepath.Join(directory, "state"), "initialization_cwd": directory,
		"control":               map[string]string{"target": "gateway.test:7100", "bearer": "control-placeholder"},
		"ingress":               map[string]string{"target": "history.test:8081", "bearer": "ingress-placeholder"},
		"platform_instructions": "Test guidance.",
		"runner_environment":    map[string]any{"values": map[string]string{"TOOL_ROUTE": "http://tools.test"}},
		"harness_environment":   map[string]any{"inherit": []string{"PATH", "TOOL_ROUTE"}, "values": map[string]string{"HOME": directory}},
		"codex":                 map[string]string{"binary": "/opt/tools/codex", "base_url": "http://models.test/v1", "token_env": "RUNNER_TEST_PROVIDER_TOKEN"},
	}
	return value, configPath, launchPath
}

func configurationVMEnvironment() map[string]any {
	return map[string]any{"kind": "vm", "agent_uid": 107, "agent_gid": 107, "block_devices": []string{"/dev/test-workspace"}, "resources": validGuestResources()}
}

func configurationVMExecutor(environment map[string]any) {
	environment["executor"] = map[string]any{"url": "ws://127.0.0.1:4501"}
	resources := environment["resources"].(map[string]any)
	resources["executor"] = map[string]any{"cpu_quota_micros": 100000, "memory_max_bytes": 67108864, "pids_max": 128, "read_bps": 52428800, "write_bps": 20971520, "read_iops": 1000, "write_iops": 500}
}

func TestConfigurationComposesBothIsolationModesWithoutOpeningState(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	t.Setenv("PATH", "/image/bin")
	t.Setenv("UNDECLARED_SECRET", "must-not-inherit")
	for _, kind := range []string{"pod-local", "pod-executor", "vm-local", "vm-executor"} {
		t.Run(kind, func(t *testing.T) {
			value, path, launchPath := configurationFixture(t)
			switch kind {
			case "pod-executor":
				value["environment"] = map[string]any{"kind": "pod", "executor": map[string]any{"url": "ws://127.0.0.1:4501", "socket": "/run/test/executor.sock"}}
			case "vm-local", "vm-executor":
				environment := configurationVMEnvironment()
				if kind == "vm-executor" {
					configurationVMExecutor(environment)
				}
				value["environment"] = environment
			}
			writeJSONFixture(t, path, value)
			config, control, ingress, environment, err := loadConfiguration(path, launchPath)
			if err != nil {
				t.Fatal(err)
			}
			if config.VMProcessPolicy != nil {
				resources, err := guestResourcesFromMap(value["environment"].(map[string]any)["resources"].(map[string]any))
				if err != nil {
					t.Fatal(err)
				}
				if config.VMProcessPolicy.AggregateLimits != resources.Aggregate || config.VMProcessPolicy.ProcessLimits != resources.Process {
					t.Fatal("harness limits differ from VM resource policy")
				}
				if resources.Executor != nil && (config.CodexExecServer.Isolation.AggregateLimits != *resources.Executor || config.CodexExecServer.Isolation.ProcessLimits != *resources.Executor) {
					t.Fatal("executor limits differ from VM resource policy")
				}
			}
			if control.Target != "gateway.test:7100" || ingress.Target != "history.test:8081" {
				t.Fatalf("endpoints = %v %v", control, ingress)
			}
			want := map[string]string{"PATH": "/image/bin", "TOOL_ROUTE": "http://tools.test", "HOME": filepath.Dir(path)}
			if !reflect.DeepEqual(config.HarnessEnvironment, want) {
				t.Fatalf("child environment = %v, want %v", config.HarnessEnvironment, want)
			}
			if environment["TOOL_ROUTE"] != "http://tools.test" || config.Codex.Token != "provider-secret" {
				t.Fatal("environment/provider configuration was not composed")
			}
			if config.Launch.PlatformInstructions != "Test guidance." || config.Launch.Bootstrap == nil || *config.Launch.Bootstrap != "echo initialized" {
				t.Fatalf("launch = %+v", config.Launch)
			}
			if config.SessionReconcileConcurrency != defaultSessionReconcileConcurrency {
				t.Fatalf("session reconcile concurrency = %d, want default %d", config.SessionReconcileConcurrency, defaultSessionReconcileConcurrency)
			}
			if _, err := os.Stat(config.StateDir); !os.IsNotExist(err) {
				t.Fatalf("configuration opened state: %v", err)
			}
			if (config.CodexExecServer == nil) != (kind == "pod-local" || kind == "vm-local") || (config.VMProcessPolicy != nil) != (kind == "vm-local" || kind == "vm-executor") {
				t.Fatalf("incorrect isolation for %s", kind)
			}
		})
	}
}

func TestConfigurationRejectsInvalidPolicyBeforeEnvironmentChanges(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	for _, test := range []struct {
		name   string
		update func(map[string]any)
	}{
		{"unknown field", func(v map[string]any) { v["unexpected"] = true }},
		{"zero session reconcile concurrency", func(v map[string]any) { v["session_reconcile_concurrency"] = 0 }},
		{"negative session reconcile concurrency", func(v map[string]any) { v["session_reconcile_concurrency"] = -1 }},
		{"old version", func(v map[string]any) { v["version"] = 1 }},
		{"relative state", func(v map[string]any) { v["state_dir"] = "relative" }},
		{"missing initialization cwd", func(v map[string]any) { delete(v, "initialization_cwd") }},
		{"empty workspace root", func(v map[string]any) { v["workspace_root"] = "" }},
		{"missing bearer", func(v map[string]any) { v["control"] = map[string]string{"target": "gateway.test:7100"} }},
		{"bad endpoint", func(v map[string]any) {
			v["ingress"] = map[string]string{"target": "host:notaport", "bearer": "placeholder"}
		}},
		{"partial provider", func(v map[string]any) { v["codex"] = map[string]string{"binary": "/opt/codex"} }},
		{"missing provider token", func(v map[string]any) { v["codex"].(map[string]string)["token_env"] = "ABSENT_PROVIDER_TOKEN" }},
		{"missing environment", func(v map[string]any) { delete(v, "environment") }},
		{"unknown environment", func(v map[string]any) { v["environment"] = map[string]any{"kind": "unknown"} }},
		{"partial container executor", func(v map[string]any) {
			v["environment"] = map[string]any{"kind": "pod", "executor": map[string]any{"url": "ws://127.0.0.1:4501"}}
		}},
		{"VM policy in Pod", func(v map[string]any) {
			v["environment"] = map[string]any{"kind": "pod", "resources": validGuestResources()}
		}},
		{"container policy in VM", func(v map[string]any) {
			env := configurationVMEnvironment()
			configurationVMExecutor(env)
			env["executor"].(map[string]any)["socket"] = "/test/socket"
			v["environment"] = env
		}},
		{"VM endpoint without budget", func(v map[string]any) {
			env := configurationVMEnvironment()
			env["executor"] = map[string]any{"url": "ws://127.0.0.1:4501"}
			v["environment"] = env
		}},
		{"VM budget without endpoint", func(v map[string]any) {
			env := configurationVMEnvironment()
			configurationVMExecutor(env)
			delete(env, "executor")
			v["environment"] = env
		}},
		{"VM executor exceeds service headroom", func(v map[string]any) {
			env := configurationVMEnvironment()
			configurationVMExecutor(env)
			env["resources"].(map[string]any)["executor"].(map[string]any)["memory_max_bytes"] = 67108865
			v["environment"] = env
		}},
		{"obsolete VM executor root", func(v map[string]any) {
			env := configurationVMEnvironment()
			configurationVMExecutor(env)
			env["executor"].(map[string]any)["root"] = "/old/executor/root"
			v["environment"] = env
		}},
		{"executor without Codex", func(v map[string]any) {
			delete(v, "codex")
			env := configurationVMEnvironment()
			configurationVMExecutor(env)
			v["environment"] = env
		}},
		{"manual execution override", func(v map[string]any) {
			v["harness_environment"] = map[string]any{"values": map[string]string{"CODEX_EXEC_SERVER_URL": "ws://127.0.0.1:9999"}}
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv("TOOL_ROUTE", "original")
			value, path, launchPath := configurationFixture(t)
			test.update(value)
			writeJSONFixture(t, path, value)
			if _, _, _, err := LoadConfiguration(path, launchPath); err == nil {
				t.Fatal("accepted invalid configuration")
			}
			if os.Getenv("TOOL_ROUTE") != "original" {
				t.Fatal("invalid configuration modified process environment")
			}
		})
	}
}

func TestConfigurationUsesExplicitSessionReconcileConcurrency(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	value, path, launchPath := configurationFixture(t)
	value["session_reconcile_concurrency"] = 2048
	writeJSONFixture(t, path, value)

	config, _, _, _, err := loadConfiguration(path, launchPath)
	if err != nil {
		t.Fatal(err)
	}
	if config.SessionReconcileConcurrency != 2048 {
		t.Fatalf("session reconcile concurrency = %d, want 2048", config.SessionReconcileConcurrency)
	}
}

func TestConfigurationRejectsLaunchFieldsOutsideControllerContract(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	value, path, launchPath := configurationFixture(t)
	writeJSONFixture(t, path, value)
	for _, data := range []string{`{"sandbox":{"namespace":"test","name":"sandbox"}}`, `{"sandbox":{"namespace":"test","name":"sandbox","uid":"uid"},"bootstrap":""}`, `{"sandbox":{"namespace":"test","name":"sandbox","uid":"uid"},"platform_instructions":"override"}`} {
		var launch any
		if err := json.Unmarshal([]byte(data), &launch); err != nil {
			t.Fatal(err)
		}
		writeJSONFixture(t, launchPath, launch)
		if _, _, _, _, err := loadConfiguration(path, launchPath); err == nil {
			t.Fatalf("accepted invalid launch %s", data)
		}
	}
}
