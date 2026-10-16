// The first-stage init for the local Firecracker layering probe.
//
// This deliberately starts the child with only a statically linked Codex binary in its root.
// A tool or runtime dependency is added only after an observed failure identifies it.
package main

import (
	"fmt"
	"os"
	"os/exec"
	"syscall"
)

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stdout, "runner-layering init: "+format+"\n", args...)
	os.Exit(1)
}

func mountToolsFilesystem(device, destination string) {
	if err := os.MkdirAll(destination, 0o755); err != nil {
		fatal("create mountpoint %s: %v", destination, err)
	}
	if err := syscall.Mount(device, destination, "squashfs", syscall.MS_RDONLY|syscall.MS_NOSUID, ""); err != nil {
		fatal("mount tools filesystem %s at %s: %v", device, destination, err)
	}
}

func runProbe(name, path string, args ...string) error {
	command := exec.Command(path, args...)
	command.Dir = "/workspace"
	command.Env = []string{
		"HOME=/home/agent",
		"CODEX_HOME=/home/agent/.codex",
		"CLAUDE_CONFIG_DIR=/home/agent/.claude",
		"PATH=/usr/bin:/bin:/opt/bin",
		"TMPDIR=/tmp",
	}
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	command.SysProcAttr = &syscall.SysProcAttr{
		Chroot:     "/tools",
		Cloneflags: syscall.CLONE_NEWNS,
		Credential: &syscall.Credential{
			Uid:         107,
			Gid:         107,
			NoSetGroups: true,
		},
	}
	if err := command.Run(); err != nil {
		return fmt.Errorf("pinned %s process failed: %w", name, err)
	}
	fmt.Fprintf(os.Stdout, "runner-layering: pinned %s exited successfully\n", name)
	return nil
}

func runClaudeRootComparison() error {
	command := exec.Command("/opt/claude", "--version")
	command.Dir = "/workspace"
	command.Env = []string{
		"HOME=/home/agent",
		"CLAUDE_CONFIG_DIR=/home/agent/.claude",
		"PATH=/usr/bin:/bin:/opt/bin",
		"TMPDIR=/tmp",
	}
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	command.SysProcAttr = &syscall.SysProcAttr{Chroot: "/tools"}
	if err := command.Run(); err != nil {
		return fmt.Errorf("Claude process without child uid/mount namespaces failed: %w", err)
	}
	fmt.Fprintln(os.Stdout, "runner-layering: Claude root chroot comparison exited successfully")
	return nil
}

func main() {
	if os.Getpid() != 1 {
		fatal("expected PID 1, got %d", os.Getpid())
	}
	if err := os.MkdirAll("/dev", 0o755); err != nil {
		fatal("create /dev: %v", err)
	}
	if err := syscall.Mount("devtmpfs", "/dev", "devtmpfs", syscall.MS_NOSUID|syscall.MS_STRICTATIME, "mode=0755"); err != nil {
		fatal("mount devtmpfs: %v", err)
	}
	if err := os.MkdirAll("/proc", 0o555); err != nil {
		fatal("create /proc: %v", err)
	}
	if err := syscall.Mount("proc", "/proc", "proc", syscall.MS_NOSUID|syscall.MS_NODEV|syscall.MS_NOEXEC, ""); err != nil {
		fatal("mount procfs: %v", err)
	}
	console, err := os.OpenFile("/dev/console", os.O_RDWR, 0)
	if err != nil {
		fatal("open guest console: %v", err)
	}
	for _, fd := range []uintptr{0, 1, 2} {
		if err := syscall.Dup2(int(console.Fd()), int(fd)); err != nil {
			fatal("attach guest console fd %d: %v", fd, err)
		}
	}

	// Firecracker attaches two independent SquashFS drives: the tools root and the harness payload.
	// The harness filesystem is mounted below /tools/opt, allowing its pinned binaries to resolve
	// dynamic libraries from the tools root without copying either layer into the other.
	if err := os.MkdirAll("/tools", 0o755); err != nil {
		fatal("create tools root mountpoint: %v", err)
	}
	mountToolsFilesystem("/dev/vda", "/tools")
	mountToolsFilesystem("/dev/vdb", "/tools/opt")

	// This probe keeps these paths tmpfs-backed; the runner integration test separately exercises
	// state retained across native harness restarts.
	for _, mount := range []struct {
		name string
		path string
		data string
	}{
		{name: "Codex home", path: "/tools/home/agent/.codex", data: "mode=0700,uid=107,gid=107"},
		{name: "Claude home", path: "/tools/home/agent/.claude", data: "mode=0700,uid=107,gid=107"},
		{name: "workspace", path: "/tools/workspace", data: "mode=0770,uid=107,gid=107"},
		{name: "temporary files", path: "/tools/tmp", data: "mode=1777,uid=107,gid=107"},
	} {
		if err := syscall.Mount("tmpfs", mount.path, "tmpfs", syscall.MS_NOSUID|syscall.MS_NODEV, mount.data); err != nil {
			fatal("mount %s: %v", mount.name, err)
		}
	}
	if err := syscall.Mount(
		"proc",
		"/tools/proc",
		"proc",
		syscall.MS_NOSUID|syscall.MS_NODEV|syscall.MS_NOEXEC|syscall.MS_RDONLY,
		"",
	); err != nil {
		fatal("mount read-only child procfs: %v", err)
	}

	fmt.Fprintln(os.Stdout, "runner-layering: mounted separate read-only tools and harness filesystems")
	if err := runProbe("Bash tools", "/usr/bin/bash", "-c", "printf 'LAYERED_BASH_OK\\n'"); err != nil {
		fatal("%v", err)
	}
	if err := runProbe("Codex", "/opt/bin/codex", "--version"); err != nil {
		fatal("%v", err)
	}
	if err := runProbe("Claude", "/opt/claude", "--version"); err != nil {
		fmt.Fprintf(os.Stdout, "runner-layering: Claude probe completed with an error: %v\n", err)
	} else {
		fmt.Fprintln(os.Stdout, "runner-layering: Claude probe completed successfully")
	}
	if err := runClaudeRootComparison(); err != nil {
		fmt.Fprintf(os.Stdout, "runner-layering: Claude root chroot comparison failed: %v\n", err)
	}
	fmt.Fprintln(os.Stdout, "runner-layering: Claude root chroot comparison completed")
	fmt.Fprintln(os.Stdout, "runner-layering: child exited; parent init still owns control")
	for {
		select {}
	}
}
