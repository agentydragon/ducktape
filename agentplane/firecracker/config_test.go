package firecracker

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

var configEnvironment = []string{
	"POD_UID",
	"SANDBOX_UID",
	"FIRECRACKER_VCPU_COUNT",
	"FIRECRACKER_MEMORY_MIB",
	"FIRECRACKER_BOOT_IMAGE",
	"FIRECRACKER_RUNNER_IMAGE",
	"WORKSPACE_KIND",
}

func clearConfigEnvironment(t *testing.T) {
	t.Helper()
	for _, name := range configEnvironment {
		value, hadValue := os.LookupEnv(name)
		if err := os.Unsetenv(name); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if hadValue {
				_ = os.Setenv(name, value)
			} else {
				_ = os.Unsetenv(name)
			}
		})
	}
}

func writeConfig(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

const validConfigJSON = `{
  "pod_uid": "pod-from-file",
  "sandbox_uid": "sandbox-from-file",
  "vcpu_count": 2,
  "memory_mib": 512,
  "boot_image": "registry.invalid/boot@sha256:abc",
  "runner_image": "registry.invalid/runner@sha256:def",
  "workspace_kind": "RawDiskImage"
}`

func TestLoadConfigDefaultsAndEnvironmentOverrides(t *testing.T) {
	clearConfigEnvironment(t)
	t.Setenv("POD_UID", "pod-from-env")
	t.Setenv("FIRECRACKER_VCPU_COUNT", "4")
	t.Setenv("WORKSPACE_KIND", "BlockFilesystem")
	config, err := LoadConfig(writeConfig(t, validConfigJSON))
	if err != nil {
		t.Fatal(err)
	}
	if config.PodUID != "pod-from-env" || config.SandboxUID != "sandbox-from-file" || config.VCPUCount != 4 ||
		config.WorkspaceKind != WorkspaceKindBlockFilesystem || config.BootDirectory != "/images/boot" ||
		config.RunnerDirectory != "/images/runner" || config.StateSizeMiB != 2048 || config.WorkspaceSizeMiB != 8192 ||
		config.RelayPort != 3128 || len(config.PlatformRoutePorts) != 0 {
		t.Fatalf("unexpected loaded config: %+v", config)
	}
}

func TestLoadConfigRejectsNonObjectsUnknownNullAndTrailingJSON(t *testing.T) {
	clearConfigEnvironment(t)
	for _, contents := range []string{
		`[]`,
		strings.TrimSuffix(validConfigJSON, "}") + `, "unknown": true}`,
		strings.Replace(validConfigJSON, `"memory_mib": 512`, `"memory_mib": null`, 1),
		validConfigJSON + `{}`,
	} {
		t.Run(contents, func(t *testing.T) {
			if _, err := LoadConfig(writeConfig(t, contents)); err == nil {
				t.Fatal("invalid JSON configuration was accepted")
			}
		})
	}
}

func TestLoadConfigValidatesResourcesAndRoutePorts(t *testing.T) {
	clearConfigEnvironment(t)
	for _, mutation := range []struct {
		name string
		from string
		to   string
	}{
		{name: "too many vcpus", from: `"vcpu_count": 2`, to: `"vcpu_count": 33`},
		{name: "too little memory", from: `"memory_mib": 512`, to: `"memory_mib": 127`},
		{name: "unsupported workspace", from: `"RawDiskImage"`, to: `"DirectoryTree"`},
		{name: "duplicate route", from: `"workspace_kind": "RawDiskImage"`, to: `"workspace_kind": "RawDiskImage", "platform_route_ports": [8443, 8443]`},
		{name: "control route", from: `"workspace_kind": "RawDiskImage"`, to: `"workspace_kind": "RawDiskImage", "platform_route_ports": [9090]`},
	} {
		t.Run(mutation.name, func(t *testing.T) {
			contents := strings.Replace(validConfigJSON, mutation.from, mutation.to, 1)
			if _, err := LoadConfig(writeConfig(t, contents)); err == nil {
				t.Fatalf("invalid configuration was accepted: %s", contents)
			}
		})
	}
}

func TestLoadConfigRejectsInvalidEnvironmentNumber(t *testing.T) {
	clearConfigEnvironment(t)
	t.Setenv("FIRECRACKER_MEMORY_MIB", "large")
	if _, err := LoadConfig(writeConfig(t, validConfigJSON)); err == nil {
		t.Fatal("invalid numeric environment override was accepted")
	}
}
