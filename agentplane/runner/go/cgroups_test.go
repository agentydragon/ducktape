package runner

import (
	"os"
	"path/filepath"
	"testing"
)

func TestWriteCgroupLimitsAppliesAggregateAndProcessBudgets(t *testing.T) {
	for _, test := range []struct {
		name   string
		limits map[string]string
	}{
		{name: "aggregate", limits: map[string]string{
			"cpu.max":          "400000 100000",
			"memory.max":       "4294967296",
			"memory.oom.group": "1",
			"memory.swap.max":  "0",
			"pids.max":         "768",
		}},
		{name: "process", limits: map[string]string{
			"cpu.max":          "300000 100000",
			"memory.max":       "3221225472",
			"memory.oom.group": "1",
			"memory.swap.max":  "0",
			"pids.max":         "512",
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			directory := t.TempDir()
			for name := range test.limits {
				if err := os.WriteFile(filepath.Join(directory, name), nil, 0o600); err != nil {
					t.Fatal(err)
				}
			}
			ioControl := filepath.Join(directory, "io.max")
			if err := os.WriteFile(ioControl, nil, 0o600); err != nil {
				t.Fatal(err)
			}
			if err := writeCgroupLimits(directory, test.limits, []string{"8:16"}); err != nil {
				t.Fatal(err)
			}
			for name, value := range test.limits {
				got, err := os.ReadFile(filepath.Join(directory, name))
				if err != nil || string(got) != value+"\n" {
					t.Errorf("%s = %q, %v; want %q", name, got, err, value+"\n")
				}
			}
			got, err := os.ReadFile(ioControl)
			if err != nil || string(got) != "8:16 "+ioLimit+"\n" {
				t.Errorf("io.max = %q, %v; want device throttle line", got, err)
			}
		})
	}
}

func TestBlockDeviceIDsRejectsNonBlockTargets(t *testing.T) {
	path := filepath.Join(t.TempDir(), "regular-file")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	cgroups := &AgentCgroups{BlockDevices: []string{path}}
	if _, err := cgroups.blockDeviceIDs(); err == nil {
		t.Fatal("blockDeviceIDs accepted a regular file")
	}
}

func TestCgroupIsEmptyRequiresAZeroPopulatedEvent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cgroup.events")
	for _, test := range []struct {
		contents string
		want     bool
	}{
		{contents: "populated 0\nfrozen 0\n", want: true},
		{contents: "populated 1\nfrozen 0\n", want: false},
		{contents: "frozen 0\n", want: false},
	} {
		if err := os.WriteFile(path, []byte(test.contents), 0o600); err != nil {
			t.Fatal(err)
		}
		got, err := cgroupIsEmpty(path)
		if err != nil || got != test.want {
			t.Errorf("cgroupIsEmpty(%q) = (%t, %v), want %t", test.contents, got, err, test.want)
		}
	}
}
