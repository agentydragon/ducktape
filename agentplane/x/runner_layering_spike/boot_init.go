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

	// The two writable paths are mounted outside the read-only tool root. This first probe keeps
	// them tmpfs-backed; the runner integration test exercises state retained across restarts.
	for _, path := range []string{"/tools/home/agent/.codex", "/tools/workspace", "/tools/tmp"} {
		if err := os.MkdirAll(path, 0o755); err != nil {
			fatal("create mountpoint %s: %v", path, err)
		}
	}
	if err := syscall.Mount("/tools", "/tools", "", syscall.MS_BIND|syscall.MS_REC, ""); err != nil {
		fatal("bind tool root: %v", err)
	}
	if err := syscall.Mount("", "/tools", "", syscall.MS_BIND|syscall.MS_REMOUNT|syscall.MS_RDONLY|syscall.MS_REC, ""); err != nil {
		fatal("make tool root read-only: %v", err)
	}
	for _, mount := range []struct {
		name string
		path string
		data string
	}{
		{name: "Codex home", path: "/tools/home/agent/.codex", data: "mode=0700,uid=107,gid=107"},
		{name: "workspace", path: "/tools/workspace", data: "mode=0770,uid=107,gid=107"},
		{name: "temporary files", path: "/tools/tmp", data: "mode=1777,uid=107,gid=107"},
	} {
		if err := syscall.Mount("tmpfs", mount.path, "tmpfs", syscall.MS_NOSUID|syscall.MS_NODEV, mount.data); err != nil {
			fatal("mount %s: %v", mount.name, err)
		}
	}

	fmt.Fprintln(os.Stdout, "runner-layering: guest init started outside child root")
	command := exec.Command("/opt/bin/codex", "--version")
	command.Dir = "/workspace"
	command.Env = []string{
		"HOME=/home/agent",
		"CODEX_HOME=/home/agent/.codex",
		"PATH=/opt",
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
		fatal("pinned Codex process failed: %v", err)
	}
	fmt.Fprintln(os.Stdout, "runner-layering: child exited; parent init still owns control")
	for {
		select {}
	}
}
