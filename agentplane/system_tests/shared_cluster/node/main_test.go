package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLimitNodeProcessesCapsAggregateAndPreservesStricterLimit(t *testing.T) {
	for _, test := range []struct {
		before string
		after  string
		valid  bool
	}{
		{before: "max", after: "8192", valid: true},
		{before: "16384", after: "8192", valid: true},
		{before: "4096", after: "4096", valid: true},
		{before: "0", valid: false},
		{before: "invalid", valid: false},
	} {
		t.Run(test.before, func(t *testing.T) {
			root := t.TempDir()
			filename := filepath.Join(root, "pids.max")
			if err := os.WriteFile(filename, []byte(test.before), 0o644); err != nil {
				t.Fatal(err)
			}
			err := limitNodeProcesses(root)
			if (err == nil) != test.valid {
				t.Fatalf("limitNodeProcesses(%q): %v", test.before, err)
			}
			if test.valid {
				got, err := os.ReadFile(filename)
				if err != nil || string(got) != test.after {
					t.Fatalf("pids.max = %q, %v; want %q", got, err, test.after)
				}
			}
		})
	}
}

func TestCurrentCgroupDirectoryResolvesMembershipUnderMountRoot(t *testing.T) {
	mountinfo := "29 23 0:27 /kubepods/pod\\040name /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
	membership := "0::/kubepods/pod name/container.scope\n"
	directory, err := currentCgroupDirectory(mountinfo, membership, "/sys/fs/cgroup")
	if err != nil {
		t.Fatal(err)
	}
	if want := "/sys/fs/cgroup/container.scope"; directory != want {
		t.Fatalf("current cgroup directory = %q, want %q", directory, want)
	}
}

func TestCurrentCgroupDirectoryRejectsMembershipOutsideMountRoot(t *testing.T) {
	mountinfo := "29 23 0:27 /kubepods/pod-a /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
	membership := "0::/kubepods/pod-b/container.scope\n"
	if _, err := currentCgroupDirectory(mountinfo, membership, "/sys/fs/cgroup"); err == nil {
		t.Fatal("membership outside the mounted subtree was accepted")
	}
}

func TestFindCgroup2MountUsesTopmostMountAtExpectedPath(t *testing.T) {
	mountinfo := "" +
		"29 23 0:27 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n" +
		"30 29 0:27 /kubepods/pod-a /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
	mount, err := findCgroup2Mount(mountinfo, "/sys/fs/cgroup")
	if err != nil {
		t.Fatal(err)
	}
	if mount.root != "/kubepods/pod-a" {
		t.Fatalf("topmost cgroup mount root = %q, want /kubepods/pod-a", mount.root)
	}
}

func TestParseUnifiedCgroupRequiresOneV2Membership(t *testing.T) {
	for _, test := range []struct {
		name    string
		content string
		want    string
		valid   bool
	}{
		{name: "unified", content: "0::/kubepods/pod-a/container.scope\n", want: "/kubepods/pod-a/container.scope", valid: true},
		{name: "legacy only", content: "2:cpu:/kubepods/pod-a\n", valid: false},
		{name: "duplicate unified", content: "0::/a\n0::/b\n", valid: false},
		{name: "non-clean", content: "0::/a/../b\n", valid: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := parseUnifiedCgroup(test.content)
			if test.valid {
				if err != nil || got != test.want {
					t.Fatalf("parseUnifiedCgroup() = %q, %v; want %q", got, err, test.want)
				}
			} else if err == nil {
				t.Fatalf("parseUnifiedCgroup(%q) = %q, want an error", test.content, got)
			}
		})
	}
}

func TestValidateInitialCgroupRejectsNamespaceRootAndRestrictedMount(t *testing.T) {
	for _, test := range []struct {
		name       string
		mount      cgroupMount
		membership string
		wantError  bool
	}{
		{name: "host hierarchy container cgroup", mount: cgroupMount{root: "/", mountpoint: "/sys/fs/cgroup"}, membership: "/kubepods/pod-a/container.scope"},
		{name: "namespace root", mount: cgroupMount{root: "/", mountpoint: "/sys/fs/cgroup"}, membership: "/", wantError: true},
		{name: "restricted mount", mount: cgroupMount{root: "/kubepods/pod-a", mountpoint: "/sys/fs/cgroup"}, membership: "/kubepods/pod-a/container.scope", wantError: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			err := validateInitialCgroup(test.mount, test.membership)
			if (err != nil) != test.wantError {
				t.Fatalf("validateInitialCgroup() error = %v, wantError %v", err, test.wantError)
			}
		})
	}
}

func TestParseFiniteResourceLimits(t *testing.T) {
	for _, test := range []struct {
		name  string
		value string
		parse func(string) error
		valid bool
	}{
		{name: "finite memory", value: "536870912\n", parse: parseFiniteMemoryLimit, valid: true},
		{name: "unlimited memory", value: "max\n", parse: parseFiniteMemoryLimit},
		{name: "zero memory", value: "0\n", parse: parseFiniteMemoryLimit},
		{name: "malformed memory", value: "512M\n", parse: parseFiniteMemoryLimit},
		{name: "finite CPU", value: "150000 100000\n", parse: parseFiniteCPUQuota, valid: true},
		{name: "unlimited CPU", value: "max 100000\n", parse: parseFiniteCPUQuota},
		{name: "zero CPU quota", value: "0 100000\n", parse: parseFiniteCPUQuota},
		{name: "zero CPU period", value: "100000 0\n", parse: parseFiniteCPUQuota},
		{name: "malformed CPU", value: "100000\n", parse: parseFiniteCPUQuota},
	} {
		t.Run(test.name, func(t *testing.T) {
			err := test.parse(test.value)
			if (err == nil) != test.valid {
				t.Fatalf("parse(%q) error = %v, valid = %v", test.value, err, test.valid)
			}
		})
	}
}

func TestValidateCgroupBoundsRequiresLimitsAndDelegatedControllers(t *testing.T) {
	for _, test := range []struct {
		name        string
		memory      string
		cpu         string
		controllers string
		wantError   bool
	}{
		{name: "finite and available", memory: "536870912\n", cpu: "150000 100000\n", controllers: "cpu memory pids\n"},
		{name: "unlimited memory", memory: "max\n", cpu: "150000 100000\n", controllers: "cpu memory pids\n", wantError: true},
		{name: "unlimited CPU", memory: "536870912\n", cpu: "max 100000\n", controllers: "cpu memory pids\n", wantError: true},
		{name: "missing pids controller", memory: "536870912\n", cpu: "150000 100000\n", controllers: "cpu memory\n", wantError: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			cgroupDir := t.TempDir()
			writeTestCgroupFile(t, cgroupDir, "memory.max", test.memory)
			writeTestCgroupFile(t, cgroupDir, "cpu.max", test.cpu)
			writeTestCgroupFile(t, cgroupDir, "cgroup.controllers", test.controllers)
			err := validateCgroupBounds(cgroupDir)
			if (err != nil) != test.wantError {
				t.Fatalf("validateCgroupBounds() error = %v, wantError %v", err, test.wantError)
			}
		})
	}
}

func writeTestCgroupFile(t *testing.T, directory, name, contents string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(directory, name), []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
}
