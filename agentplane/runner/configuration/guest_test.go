package configuration

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestGuestResourcePropertiesModeValidatesSeedBeforeOutput(t *testing.T) {
	configPath := filepath.Join(t.TempDir(), "config.json")
	writeJSONFixture(t, configPath, validGuestConfiguration(t))
	command := exec.Command(os.Args[0], vmResourcePropertiesMode, configPath)
	output, err := command.Output()
	if err != nil {
		t.Fatalf("resource properties mode failed: %v", err)
	}
	want := strings.Join([]string{
		"MemoryHigh=1207959552",
		"MemoryMax=1342177280",
		"MemorySwapMax=0",
		"CPUQuota=100%",
		"TasksMax=1024",
		"",
	}, "\n")
	if string(output) != want {
		t.Fatalf("resource properties = %q, want %q", output, want)
	}

	for _, mutate := range []func(map[string]any){
		func(v map[string]any) {
			v["environment"].(map[string]any)["resources"].(map[string]any)["service_memory_max_bytes"] = 0
		},
		func(v map[string]any) { v["environment"] = map[string]any{"kind": "pod", "supervision": "subprocess"} },
		func(v map[string]any) { v["unexpected"] = true },
		func(v map[string]any) {
			env := v["environment"].(map[string]any)
			configurationVMExecutor(env)
			delete(env, "executor")
		},
	} {
		invalid := validGuestConfiguration(t)
		mutate(invalid)
		writeJSONFixture(t, configPath, invalid)
		command = exec.Command(os.Args[0], vmResourcePropertiesMode, configPath)
		output, err = command.CombinedOutput()
		if err == nil {
			t.Fatal("resource preflight accepted invalid configuration")
		}
		if strings.Contains(string(output), "MemoryHigh=") {
			t.Fatalf("invalid configuration produced systemd properties: %q", output)
		}
	}
}

func TestGuestMemoryCapacityRequiresLiveAllocationToMatchSeed(t *testing.T) {
	resources, err := guestResourcesFromMap(validGuestResources())
	if err != nil {
		t.Fatal(err)
	}
	staleTemplateResources := resources
	staleTemplateResources.GuestMemoryBytes = 3 * 1024 * 1024 * 1024
	staleTemplateResources.OSReserveBytes = 512 * 1024 * 1024
	staleTemplateResources.ServiceMemoryHighBytes = 2304 * 1024 * 1024
	staleTemplateResources.ServiceMemoryMaxBytes = 2560 * 1024 * 1024
	for _, test := range []struct {
		name      string
		resources guestResources
		actual    int64
		wantErr   bool
	}{
		{name: "service cap fits live memory", resources: resources, actual: 1500 * 1024 * 1024},
		{name: "stale smaller VM template", resources: staleTemplateResources, actual: 1536 * 1024 * 1024, wantErr: true},
		{name: "service cap consumes all memory", resources: resources, actual: resources.ServiceMemoryMaxBytes, wantErr: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			err := validateGuestMemoryCapacity(test.resources, test.actual)
			if (err != nil) != test.wantErr {
				t.Fatalf("validateGuestMemoryCapacity(%d) error = %v, wantErr %v", test.actual, err, test.wantErr)
			}
		})
	}
}

func TestGuestMemoryTotalBytesParsesMeminfoLines(t *testing.T) {
	path := filepath.Join(t.TempDir(), "meminfo")
	if err := os.WriteFile(path, []byte("MemTotal:       1536000 kB\nHugePages_Total: 0\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := guestMemoryTotalBytes(path)
	if err != nil {
		t.Fatal(err)
	}
	if want := int64(1536000 * 1024); got != want {
		t.Fatalf("MemTotal = %d, want %d", got, want)
	}
}

func guestResourcesFromMap(value map[string]any) (guestResources, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return guestResources{}, err
	}
	var resources guestResources
	if err := decodeStrict(data, &resources); err != nil {
		return guestResources{}, err
	}
	return resources, nil
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

func validGuestConfiguration(t *testing.T) map[string]any {
	t.Helper()
	value, _, _ := configurationFixture(t)
	value["environment"] = configurationVMEnvironment()
	return value
}

func validGuestResources() map[string]any {
	return map[string]any{
		"guest_memory_bytes": 1610612736, "guest_vcpu_count": 1, "os_reserve_bytes": 268435456,
		"service_memory_high_bytes": 1207959552, "service_memory_max_bytes": 1342177280,
		"service_cpu_quota_micros": 100000, "service_pids_max": 1024,
		"aggregate": map[string]any{
			"cpu_quota_micros": 100000, "memory_max_bytes": 1140850688,
			"pids_max": 768, "read_bps": 52428800, "write_bps": 20971520,
			"read_iops": 1000, "write_iops": 500,
		},
		"process": map[string]any{
			"cpu_quota_micros": 100000, "memory_max_bytes": 805306368,
			"pids_max": 512, "read_bps": 52428800, "write_bps": 20971520,
			"read_iops": 1000, "write_iops": 500,
		},
	}
}
