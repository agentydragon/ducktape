// Package main fences a pinned Codex process in an intentionally sparse root.
// It is a test harness for the runner layering spike, not a production launcher.
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
)

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "runner-layering child: "+format+"\n", args...)
	os.Exit(125)
}

func mustMkdir(path string) {
	if err := os.MkdirAll(path, 0o755); err != nil {
		fatal("create mountpoint %s: %v", path, err)
	}
}

func mustMount(source, target, filesystem string, flags uintptr, data string) {
	if err := syscall.Mount(source, target, filesystem, flags, data); err != nil {
		fatal("mount %s on %s: %v", source, target, err)
	}
}

func main() {
	if os.Getenv("AGENTPLANE_CHILD_STAGE") != "isolated" {
		startIsolatedStage()
		return
	}
	enterIsolatedRoot()
}

func startIsolatedStage() {
	uid, gid := os.Getuid(), os.Getgid()
	executable, err := os.Executable()
	if err != nil {
		fatal("locate child fence executable: %v", err)
	}
	command := exec.Command(executable, os.Args[1:]...)
	command.Env = append(os.Environ(), "AGENTPLANE_CHILD_STAGE=isolated")
	command.Stdin, command.Stdout, command.Stderr = os.Stdin, os.Stdout, os.Stderr
	command.SysProcAttr = &syscall.SysProcAttr{
		Cloneflags: syscall.CLONE_NEWUSER | syscall.CLONE_NEWNS | syscall.CLONE_NEWPID,
		UidMappings: []syscall.SysProcIDMap{
			{ContainerID: 0, HostID: uid, Size: 1},
		},
		GidMappings: []syscall.SysProcIDMap{
			{ContainerID: 0, HostID: gid, Size: 1},
		},
		GidMappingsEnableSetgroups: false,
		Credential:                 &syscall.Credential{Uid: 0, Gid: 0, NoSetGroups: true},
	}
	if err := command.Run(); err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			os.Exit(exit.ExitCode())
		}
		fatal("start isolated child: %v", err)
	}
}

func enterIsolatedRoot() {
	root := os.Getenv("AGENTPLANE_CHILD_ROOT")
	if root == "" {
		fatal("AGENTPLANE_CHILD_ROOT is required")
	}
	root, err := filepath.Abs(root)
	if err != nil {
		fatal("resolve child root: %v", err)
	}
	root, err = filepath.EvalSymlinks(root)
	if err != nil {
		fatal("resolve child root symlinks: %v", err)
	}

	cwd := os.Getenv("AGENTPLANE_CHILD_WORKSPACE")
	if cwd == "" {
		var err error
		cwd, err = os.Getwd()
		if err != nil {
			fatal("read workspace directory: %v", err)
		}
	}
	cwd, err = filepath.EvalSymlinks(cwd)
	if err != nil {
		fatal("resolve workspace: %v", err)
	}
	codexHome := os.Getenv("CODEX_HOME")
	if codexHome == "" {
		fatal("CODEX_HOME is required")
	}
	codexHome, err = filepath.EvalSymlinks(codexHome)
	if err != nil {
		fatal("resolve Codex home: %v", err)
	}
	// The first stage used clone flags to create these namespaces before the Go runtime
	// started its worker threads. The runner and wrapper parent remain outside the child's
	// user, mount, and PID namespaces; this stage mounts the sparse root and execs Codex.
	if err := syscall.Mount("", "/", "", syscall.MS_REC|syscall.MS_PRIVATE, ""); err != nil {
		fatal("make mount propagation private: %v", err)
	}

	for _, path := range []string{
		filepath.Join(root, "workspace"),
		filepath.Join(root, "home/agent/.codex"),
		filepath.Join(root, "dev"),
		filepath.Join(root, "proc"),
		filepath.Join(root, "tmp"),
	} {
		mustMkdir(path)
	}
	if err := os.WriteFile(filepath.Join(root, "dev/null"), nil, 0o666); err != nil {
		fatal("create /dev/null mountpoint: %v", err)
	}
	mustMount(root, root, "", syscall.MS_BIND|syscall.MS_REC, "")
	mustMount("", root, "", syscall.MS_BIND|syscall.MS_REMOUNT|syscall.MS_RDONLY, "")
	mustMount("/dev/null", filepath.Join(root, "dev/null"), "", syscall.MS_BIND, "")
	mustMount(cwd, filepath.Join(root, "workspace"), "", syscall.MS_BIND|syscall.MS_REC, "")
	mustMount(codexHome, filepath.Join(root, "home/agent/.codex"), "", syscall.MS_BIND|syscall.MS_REC, "")
	mustMount("proc", filepath.Join(root, "proc"), "proc", syscall.MS_NOSUID|syscall.MS_NODEV|syscall.MS_NOEXEC, "")
	mustMount("tmpfs", filepath.Join(root, "tmp"), "tmpfs", syscall.MS_NOSUID|syscall.MS_NODEV, "mode=1777,size=64m")

	if err := syscall.Chroot(root); err != nil {
		fatal("enter child root: %v", err)
	}
	if err := os.Chdir("/workspace"); err != nil {
		fatal("enter workspace: %v", err)
	}

	// The marker is deliberately outside the workspace and Codex home. Checking its
	// path from inside the root proves runner-private files were not mounted into it.
	if marker := os.Getenv("AGENTPLANE_PRIVATE_SENTINEL"); marker != "" {
		if _, err := os.Stat(marker); err == nil {
			fatal("runner-private state is visible at %s", marker)
		} else if !os.IsNotExist(err) {
			fatal("check runner-private state visibility at %s: %v", marker, err)
		}
		fmt.Fprintln(os.Stderr, "runner-layering: runner-private state remains outside child root")
	}

	env := []string{
		"HOME=/home/agent",
		"CODEX_HOME=/home/agent/.codex",
		"PATH=/usr/bin:/bin:/opt",
	}
	for _, name := range []string{"OPENAI_API_KEY", "OPENAI_BASE_URL", "NO_PROXY"} {
		if value, exists := os.LookupEnv(name); exists {
			env = append(env, name+"="+value)
		}
	}
	if os.Getenv("OPENAI_API_KEY") == "" {
		fatal("OPENAI_API_KEY is required")
	}
	argv := append([]string{"codex"}, os.Args[1:]...)
	if err := syscall.Exec("/opt/bin/codex", argv, env); err != nil {
		fatal("exec pinned Codex with sparse environment %s: %v", strings.Join(env, ","), err)
	}
}
