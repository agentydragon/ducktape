package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

const cgroupRoot = "/sys/fs/cgroup"

func main() {
	runtime.LockOSThread()
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "shared K3s node setup: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	// K3s evacuates and delegates its cgroup only when it starts as PID 1.
	if os.Getpid() != 1 {
		return fmt.Errorf("must start K3s as PID 1")
	}
	if err := unix.Unshare(unix.CLONE_NEWNS | unix.CLONE_NEWCGROUP); err != nil {
		return fmt.Errorf("unshare mount and cgroup namespaces: %w", err)
	}
	if err := unix.Mount("", "/", "", unix.MS_REC|unix.MS_PRIVATE, ""); err != nil {
		return fmt.Errorf("make mount tree private: %w", err)
	}
	// A new cgroup2 mount is rooted at the current container's cgroup namespace.
	if err := unix.Mount("cgroup2", cgroupRoot, "cgroup2", unix.MS_NOSUID|unix.MS_NODEV|unix.MS_NOEXEC, ""); err != nil {
		return fmt.Errorf("mount container cgroup view: %w", err)
	}
	if err := configureLimits(cgroupRoot); err != nil {
		return err
	}
	if err := os.Mkdir(cgroupRoot+"/workloads", 0o755); err != nil {
		return fmt.Errorf("create kubelet cgroup root: %w", err)
	}
	if err := unix.Mount("", "/", "", unix.MS_REC|unix.MS_SHARED, ""); err != nil {
		return fmt.Errorf("share mounts within node: %w", err)
	}

	// K3s owns evacuation to /init and controller delegation; see its
	// pkg/cli/cmds/init_linux.go:EvacuateCgroup2. Do not duplicate that setup here.
	return unix.Exec("/bin/k3s", []string{"/bin/k3s", "server", "--config=/etc/rancher/k3s/config.yaml"}, os.Environ())
}

func configureLimits(root string) error {
	for name, count := range map[string]int{"memory.max": 1, "cpu.max": 2} {
		contents, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			return err
		}
		fields := strings.Fields(string(contents))
		if len(fields) != count {
			return fmt.Errorf("invalid %s: %q", name, contents)
		}
		for _, field := range fields {
			if value, err := strconv.ParseUint(field, 10, 64); err != nil || value == 0 {
				return fmt.Errorf("%s must have positive finite limits: %q", name, contents)
			}
		}
	}

	filename := filepath.Join(root, "pids.max")
	contents, err := os.ReadFile(filename)
	if err != nil {
		return err
	}
	if value := strings.TrimSpace(string(contents)); value != "max" {
		limit, err := strconv.ParseUint(value, 10, 64)
		if err != nil || limit == 0 {
			return fmt.Errorf("invalid pids.max: %q", value)
		}
		if limit <= 8192 {
			return nil // Preserve a stricter outer kubelet limit.
		}
	}
	return os.WriteFile(filename, []byte("8192"), 0)
}
