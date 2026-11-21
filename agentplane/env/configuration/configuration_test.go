package configuration

import (
	"encoding/json"
	"maps"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"go.uber.org/zap/zapcore"
)

func configurationFixture(t *testing.T) (map[string]any, string, string) {
	t.Helper()
	directory := t.TempDir()
	configPath, launchPath := filepath.Join(directory, "environment-runtime.json"), filepath.Join(directory, "launch.json")
	writeJSONFixture(t, launchPath, map[string]any{"sandbox": map[string]string{"namespace": "test", "name": "sandbox", "uid": "sandbox-uid"}, "bootstrap": "echo initialized"})
	value := map[string]any{
		"environment": map[string]any{"kind": "pod", "supervision": "pid1"},
		"version":     2, "state_dir": filepath.Join(directory, "state"), "initialization_cwd": directory,
		"control": map[string]any{"listen_address": "127.0.0.1:8090", "security": map[string]any{"mode": "mtls", "tls": map[string]string{
			"cert_file": "/run/test/tls.crt", "key_file": "/run/test/tls.key", "ca_file": "/run/test/ca.crt",
		}}},
		"ingress":                         map[string]string{"target": "history.test:8081", "bearer": "ingress-placeholder"},
		"platform_instructions":           "Test guidance.",
		"environment_runtime_environment": map[string]any{"values": map[string]string{"TOOL_ROUTE": "http://tools.test"}},
		"harness_environment":             map[string]any{"inherit": []string{"PATH", "TOOL_ROUTE"}, "values": map[string]string{"HOME": directory}},
		"codex":                           map[string]string{"binary": "/opt/tools/codex", "base_url": "http://models.test/v1", "token_env": "RUNNER_TEST_PROVIDER_TOKEN"},
	}
	return value, configPath, launchPath
}

func configurationVMEnvironment() map[string]any {
	return map[string]any{"kind": "vm", "agent_uid": 107, "agent_gid": 107, "block_devices": []string{"/dev/test-workspace"}, "resources": validGuestResources()}
}

func TestConfigurationRequiresExplicitControlSecurity(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	for _, test := range []struct {
		name     string
		security map[string]any
		valid    bool
	}{
		{name: "explicit insecure", security: map[string]any{"mode": "insecure"}, valid: true},
		{name: "missing mode", security: map[string]any{}},
		{name: "unknown mode", security: map[string]any{"mode": "automatic"}},
		{name: "mtls without credentials", security: map[string]any{"mode": "mtls"}},
		{name: "insecure with credentials", security: map[string]any{"mode": "insecure", "tls": map[string]string{
			"cert_file": "/run/test/tls.crt", "key_file": "/run/test/tls.key", "ca_file": "/run/test/ca.crt",
		}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			value, path, launch := configurationFixture(t)
			value["control"].(map[string]any)["security"] = test.security
			writeJSONFixture(t, path, value)
			_, _, _, err := load(path, launch)
			if (err == nil) != test.valid {
				t.Fatalf("load error = %v, valid = %t", err, test.valid)
			}
		})
	}
}

func TestConfigurationAcceptsModelConfigTarget(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	value, path, launch := configurationFixture(t)
	value["model_config_target"] = "127.0.0.1:3130"
	writeJSONFixture(t, path, value)
	if _, _, _, err := load(path, launch); err != nil {
		t.Fatal(err)
	}
}

func configurationVMExecutor(environment map[string]any) {
	environment["executor"] = map[string]any{"url": "ws://127.0.0.1:4501", "placement": codexexecserver.PlacementDirect}
	resources := environment["resources"].(map[string]any)
	resources["executor"] = map[string]any{"cpu_quota_micros": 100000, "memory_max_bytes": 67108864, "pids_max": 128, "read_bps": 52428800, "write_bps": 20971520, "read_iops": 1000, "write_iops": 500}
}

func configurationVMSystemdExecutor(environment map[string]any) {
	configurationVMExecutor(environment)
	environment["executor"].(map[string]any)["placement"] = codexexecserver.PlacementSystemd
	resources := environment["resources"].(map[string]any)
	server := resources["executor"].(map[string]any)
	server["memory_max_bytes"] = 33554432
	workload := maps.Clone(server)
	workload["pids_max"] = 127
	resources["executor_workload"] = workload
}

func TestConfigurationVMSystemdExecutorPlacement(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	for _, test := range []struct {
		name   string
		update func(executor, resources map[string]any)
		want   string
	}{
		{name: "separate shared workload budget"},
		{"missing workload budget", func(_, resources map[string]any) { delete(resources, "executor_workload") }, "budget must be supplied together"},
		{"direct with workload budget", func(executor, _ map[string]any) { executor["placement"] = codexexecserver.PlacementDirect }, "budget must be supplied together"},
		{"Codex with systemd", func(executor, _ map[string]any) { executor["implementation"] = codexexecserver.ImplementationCodex }, "requires native VM execution"},
		{"workload exceeds memory headroom", func(_, resources map[string]any) {
			resources["executor_workload"].(map[string]any)["memory_max_bytes"] = 33554433
		}, "remaining service memory high"},
		{"workload exhausts PID headroom", func(_, resources map[string]any) {
			resources["executor_workload"].(map[string]any)["pids_max"] = 128
		}, "leave room for Environment Runtime"},
		{"workload exceeds CPU capacity", func(_, resources map[string]any) {
			resources["executor_workload"].(map[string]any)["cpu_quota_micros"] = 100001
		}, "fit within Environment Runtime service quota"},
		{"invalid workload I/O", func(_, resources map[string]any) {
			resources["executor_workload"].(map[string]any)["read_iops"] = 0
		}, "invalid executor workload limits"},
	} {
		t.Run(test.name, func(t *testing.T) {
			value, path, launch := configurationFixture(t)
			env := configurationVMEnvironment()
			configurationVMSystemdExecutor(env)
			resources := env["resources"].(map[string]any)
			if test.update != nil {
				test.update(env["executor"].(map[string]any), resources)
			}
			value["environment"] = env
			writeJSONFixture(t, path, value)
			config, _, _, err := load(path, launch)
			if test.want != "" {
				if err == nil || !strings.Contains(err.Error(), test.want) {
					t.Fatalf("load error = %v, want %q", err, test.want)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			budgets, err := guestResourcesFromMap(resources)
			if err != nil {
				t.Fatal(err)
			}
			executor := config.CodexExecServer
			if executor.Placement != codexexecserver.PlacementSystemd || executor.Launcher.AggregateLimits != *budgets.Executor {
				t.Fatalf("resolved executor = %+v", executor)
			}
			if workload := executor.WorkloadLauncher; workload == nil || workload.Slice != supervision.CommandSlice || workload.AggregateLimits != *budgets.ExecutorWorkload {
				t.Fatalf("resolved executor workload launcher = %+v", workload)
			}
		})
	}
}

func TestConfigurationExecutorPlacementWireContract(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	for _, kind := range []string{"pod", "vm"} {
		for _, test := range []struct {
			name  string
			field map[string]any
			valid bool
		}{
			{name: "missing"},
			{name: "direct", field: map[string]any{"placement": codexexecserver.PlacementDirect}, valid: true},
			{name: "null", field: map[string]any{"placement": nil}},
			{name: "empty", field: map[string]any{"placement": ""}},
			{name: "unknown", field: map[string]any{"placement": "unknown"}},
			{name: "systemd without VM workload budget", field: map[string]any{"placement": codexexecserver.PlacementSystemd}},
		} {
			t.Run(kind+"/"+test.name, func(t *testing.T) {
				value, path, launch := configurationFixture(t)
				env := value["environment"].(map[string]any)
				if kind == "vm" {
					env = configurationVMEnvironment()
					configurationVMExecutor(env)
					delete(env["executor"].(map[string]any), "placement")
				} else {
					env["executor"] = map[string]any{"url": "ws://127.0.0.1:4501", "socket": "/test/socket"}
				}
				maps.Copy(env["executor"].(map[string]any), test.field)
				value["environment"] = env
				writeJSONFixture(t, path, value)
				config, _, _, err := load(path, launch)
				if (err == nil) != test.valid {
					t.Fatalf("load returned %v, expected valid = %t", err, test.valid)
				}
				if test.valid && (config.CodexExecServer.Placement != codexexecserver.PlacementDirect || config.CodexExecServer.WorkloadLauncher != nil) {
					t.Fatalf("resolved direct executor = %+v", config.CodexExecServer)
				}
			})
		}
	}
}

func TestConfigurationComposesBothIsolationModesWithoutSessionState(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	t.Setenv("PATH", "/image/bin")
	t.Setenv("UNDECLARED_SECRET", "must-not-inherit")
	for _, kind := range []string{"pod-local", "pod-executor", "vm-local", "vm-executor"} {
		t.Run(kind, func(t *testing.T) {
			value, path, launchPath := configurationFixture(t)
			switch kind {
			case "pod-executor":
				value["environment"] = map[string]any{"kind": "pod", "supervision": "pid1", "executor": map[string]any{"url": "ws://127.0.0.1:4501", "socket": "/run/test/executor.sock", "placement": codexexecserver.PlacementDirect}}
			case "vm-local", "vm-executor":
				environment := configurationVMEnvironment()
				if kind == "vm-executor" {
					configurationVMExecutor(environment)
				}
				value["environment"] = environment
			}
			writeJSONFixture(t, path, value)
			config, ingress, environment, err := load(path, launchPath)
			if err != nil {
				t.Fatal(err)
			}
			if config.VMProcessLauncher != nil {
				resources, err := guestResourcesFromMap(value["environment"].(map[string]any)["resources"].(map[string]any))
				if err != nil {
					t.Fatal(err)
				}
				if config.VMProcessLauncher.AggregateLimits != resources.Aggregate || config.VMProcessLauncher.ProcessLimits != resources.Process {
					t.Fatal("harness limits differ from VM resource policy")
				}
				if resources.Executor != nil && (config.CodexExecServer.Launcher.AggregateLimits != *resources.Executor || config.CodexExecServer.Launcher.ProcessLimits != *resources.Executor) {
					t.Fatal("executor limits differ from VM resource policy")
				}
			}
			wantSupervision := "pid1"
			if config.VMProcessLauncher != nil {
				wantSupervision = "systemd"
			}
			if config.Supervision != wantSupervision {
				t.Fatalf("resolved supervision = %q, want %q", config.Supervision, wantSupervision)
			}
			if config.Control.ListenAddress != "127.0.0.1:8090" || ingress.Target != "history.test:8081" {
				t.Fatalf("endpoints = %v %v", config.Control, ingress)
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
			if config.SessionReconcileConcurrency != DefaultSessionReconcileConcurrency {
				t.Fatalf("session reconcile concurrency = %d, want default %d", config.SessionReconcileConcurrency, DefaultSessionReconcileConcurrency)
			}
			if _, err := os.Stat(config.StateDir); !os.IsNotExist(err) {
				t.Fatalf("configuration opened state: %v", err)
			}
			if (config.CodexExecServer == nil) != (kind == "pod-local" || kind == "vm-local") || (config.VMProcessLauncher != nil) != (kind == "vm-local" || kind == "vm-executor") {
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
		{"missing listen address", func(v map[string]any) { delete(v["control"].(map[string]any), "listen_address") }},
		{"bad listen address", func(v map[string]any) { v["control"].(map[string]any)["listen_address"] = "host:notaport" }},
		{"missing mTLS material", func(v map[string]any) {
			v["control"].(map[string]any)["tls"] = map[string]string{"cert_file": "/run/test/tls.crt", "key_file": "/run/test/tls.key"}
		}},
		{"bad endpoint", func(v map[string]any) {
			v["ingress"] = map[string]string{"target": "host:notaport", "bearer": "placeholder"}
		}},
		{"bad model config endpoint", func(v map[string]any) {
			v["model_config_target"] = "host:notaport"
		}},
		{"partial provider", func(v map[string]any) { v["codex"] = map[string]string{"binary": "/opt/codex"} }},
		{"missing provider token", func(v map[string]any) { v["codex"].(map[string]string)["token_env"] = "ABSENT_PROVIDER_TOKEN" }},
		{"missing environment", func(v map[string]any) { delete(v, "environment") }},
		{"unknown environment", func(v map[string]any) { v["environment"] = map[string]any{"kind": "unknown"} }},
		{"missing Pod supervision", func(v map[string]any) { v["environment"] = map[string]any{"kind": "pod"} }},
		{"unknown Pod supervision", func(v map[string]any) { v["environment"] = map[string]any{"kind": "pod", "supervision": "unknown"} }},
		{"partial container executor", func(v map[string]any) {
			v["environment"] = map[string]any{"kind": "pod", "supervision": "pid1", "executor": map[string]any{"url": "ws://127.0.0.1:4501", "placement": codexexecserver.PlacementDirect}}
		}},
		{"VM launcher in Pod", func(v map[string]any) {
			v["environment"] = map[string]any{"kind": "pod", "supervision": "pid1", "resources": validGuestResources()}
		}},
		{"container executor in VM", func(v map[string]any) {
			env := configurationVMEnvironment()
			configurationVMExecutor(env)
			env["executor"].(map[string]any)["socket"] = "/test/socket"
			v["environment"] = env
		}},
		{"VM endpoint without budget", func(v map[string]any) {
			env := configurationVMEnvironment()
			env["executor"] = map[string]any{"url": "ws://127.0.0.1:4501", "placement": codexexecserver.PlacementDirect}
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
			if _, _, err := Load(path, launchPath); err == nil {
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

	config, _, _, err := load(path, launchPath)
	if err != nil {
		t.Fatal(err)
	}
	if config.SessionReconcileConcurrency != 2048 {
		t.Fatalf("session reconcile concurrency = %d, want 2048", config.SessionReconcileConcurrency)
	}
}

func TestConfigurationResolvesCodexTurnDiffNotifications(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	for _, test := range []struct {
		name  string
		value any
		want  bool
	}{
		{name: "omitted"},
		{name: "disabled", value: false},
		{name: "enabled", value: true, want: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			value, path, launch := configurationFixture(t)
			codex := map[string]any{"binary": "/opt/tools/codex", "base_url": "http://models.test/v1", "token_env": "RUNNER_TEST_PROVIDER_TOKEN"}
			if test.value != nil {
				codex["turn_diff_notifications"] = test.value
			}
			value["codex"] = codex
			writeJSONFixture(t, path, value)
			resolved, _, _, err := load(path, launch)
			if err != nil {
				t.Fatal(err)
			}
			if resolved.CodexTurnDiffNotifications != test.want {
				t.Fatalf("turn diff notifications = %t, want %t", resolved.CodexTurnDiffNotifications, test.want)
			}
		})
	}
}

func TestConfigurationResolvesIndependentCodexReadinessWindows(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	for _, test := range []struct {
		name      string
		readiness map[string]any
		wantExec  codexexecserver.Readiness
		wantApp   codexappserver.Readiness
	}{
		{
			name: "defaults",
			wantExec: codexexecserver.Readiness{
				Timeout: 20 * time.Second, ProbeTimeout: 2 * time.Second, ProbeInterval: 100 * time.Millisecond,
			},
			wantApp: codexappserver.Readiness{
				ExecServerDependencyTimeout: 30 * time.Second,
				StartupTimeout:              90 * time.Second,
				InitializeTimeout:           60 * time.Second,
			},
		},
		{
			name:      "partial independent overrides",
			readiness: map[string]any{"exec_server_ready_timeout_ms": 725, "app_server_initialize_timeout_ms": 1337},
			wantExec: codexexecserver.Readiness{
				Timeout: 725 * time.Millisecond, ProbeTimeout: 2 * time.Second, ProbeInterval: 100 * time.Millisecond,
			},
			wantApp: codexappserver.Readiness{
				ExecServerDependencyTimeout: 30 * time.Second,
				StartupTimeout:              90 * time.Second,
				InitializeTimeout:           1337 * time.Millisecond,
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			value, path, launchPath := configurationFixture(t)
			value["environment"] = map[string]any{
				"kind": "pod", "supervision": "pid1",
				"executor": map[string]any{"url": "ws://127.0.0.1:4501", "socket": "/run/test/executor.sock", "placement": codexexecserver.PlacementDirect},
			}
			if test.readiness != nil {
				value["codex_readiness"] = test.readiness
			}
			writeJSONFixture(t, path, value)
			resolved, _, _, err := load(path, launchPath)
			if err != nil {
				t.Fatal(err)
			}
			if resolved.CodexExecServer == nil || resolved.CodexExecServer.Readiness != test.wantExec {
				t.Fatalf("exec-server readiness = %+v, want %+v", resolved.CodexExecServer, test.wantExec)
			}
			if resolved.CodexAppServerReadiness != test.wantApp {
				t.Fatalf("app-server readiness = %+v, want %+v", resolved.CodexAppServerReadiness, test.wantApp)
			}
		})
	}
}

func TestConfigurationRejectsInvalidCodexReadinessMilliseconds(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	fields := []string{
		"exec_server_ready_timeout_ms",
		"exec_server_probe_timeout_ms",
		"exec_server_probe_interval_ms",
		"exec_server_dependency_timeout_ms",
		"app_server_start_timeout_ms",
		"app_server_initialize_timeout_ms",
	}
	invalidValues := []struct {
		name  string
		value any
	}{
		{name: "zero", value: 0},
		{name: "negative", value: -1},
		{name: "duration overflow", value: maxGoDurationMilliseconds + 1},
		{name: "int64 overflow", value: json.Number("9223372036854775808")},
	}
	for _, field := range fields {
		for _, invalid := range invalidValues {
			t.Run(field+"/"+invalid.name, func(t *testing.T) {
				value, path, launchPath := configurationFixture(t)
				value["codex_readiness"] = map[string]any{field: invalid.value}
				writeJSONFixture(t, path, value)
				if _, _, _, err := load(path, launchPath); err == nil {
					t.Fatal("accepted invalid readiness timeout")
				}
			})
		}
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
		if _, _, _, err := load(path, launchPath); err == nil {
			t.Fatalf("accepted invalid launch %s", data)
		}
	}
}

func TestConfigurationPreservesExplicitPodPID1Mode(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	value, path, launch := configurationFixture(t)
	value["environment"] = map[string]any{"kind": "pod", "supervision": "pid1"}
	writeJSONFixture(t, path, value)
	config, _, _, err := load(path, launch)
	if err != nil {
		t.Fatal(err)
	}
	if config.Supervision != "pid1" || !config.PodPID1 {
		t.Fatalf("Pod PID1 mode was not preserved: supervision=%q pid1=%v", config.Supervision, config.PodPID1)
	}
}

func TestConfigurationLogLevel(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	for _, test := range []struct {
		name       string
		configured *string
		want       zapcore.Level
		valid      bool
	}{
		{name: "omitted", want: zapcore.InfoLevel, valid: true},
		{name: "debug", configured: new("debug"), want: zapcore.DebugLevel, valid: true},
		{name: "info", configured: new("info"), want: zapcore.InfoLevel, valid: true},
		{name: "warn", configured: new("warn"), want: zapcore.WarnLevel, valid: true},
		{name: "error", configured: new("error"), want: zapcore.ErrorLevel, valid: true},
		{name: "empty", configured: new("")},
		{name: "unknown", configured: new("trace")},
		{name: "fatal", configured: new("fatal")},
	} {
		t.Run(test.name, func(t *testing.T) {
			value, path, launch := configurationFixture(t)
			if test.configured != nil {
				value["log_level"] = *test.configured
			}
			writeJSONFixture(t, path, value)
			config, _, _, err := load(path, launch)
			if (err == nil) != test.valid {
				t.Fatalf("load error = %v, valid = %t", err, test.valid)
			}
			if err == nil && config.LogLevel != test.want {
				t.Fatalf("log level = %v, want %v", config.LogLevel, test.want)
			}
		})
	}
}

func TestVMExecutionLimits(t *testing.T) {
	t.Setenv("RUNNER_TEST_PROVIDER_TOKEN", "provider-secret")
	value, path, launch := configurationFixture(t)
	env := configurationVMEnvironment()
	configurationVMExecutor(env)
	executor := env["executor"].(map[string]any)
	executor["limits"] = map[string]any{"native": map[string]any{"max_sessions": 3}}
	value["environment"] = env
	writeJSONFixture(t, path, value)
	config, _, _, err := load(path, launch)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(config.CodexExecServer.LimitsJSON, `"max_sessions":3`) {
		t.Fatalf("limits = %q", config.CodexExecServer.LimitsJSON)
	}
	executor["implementation"] = "codex"
	writeJSONFixture(t, path, value)
	if _, _, _, err := load(path, launch); err == nil {
		t.Fatal("accepted native limits with Codex backend")
	}
	executor["implementation"] = "native"
	executor["limits"] = map[string]any{"native": map[string]any{"max_sessions": 0}}
	writeJSONFixture(t, path, value)
	if _, _, _, err := load(path, launch); err == nil {
		t.Fatal("accepted invalid limit")
	}
}
