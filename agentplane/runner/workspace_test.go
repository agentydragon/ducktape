package runner

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestEnsureWorkspacePreservesExistingOwnership(t *testing.T) {
	base, err := os.MkdirTemp("/tmp", "agentplane-workspace-")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(base, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(base) })
	root := filepath.Join(base, "workspaces")
	if err := os.Mkdir(root, 0o777); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(root, 0o777); err != nil {
		t.Fatal(err)
	}
	uid, gid := uint32(os.Geteuid()), uint32(os.Getegid())
	cwd := filepath.Join(root, "new", "repository")
	created, err := EnsureWorkspace(context.Background(), cwd, root, nil)
	if err != nil {
		t.Fatal(err)
	}
	if created != cwd {
		t.Fatalf("EnsureWorkspace path = %q, want %q", created, cwd)
	}
	assertOwner(t, cwd, uid, gid)
	assertOwner(t, filepath.Join(root, "new"), uid, gid)

	git, err := exec.LookPath("git")
	if err != nil {
		t.Fatalf("Git must be available in the Linux test worker: %v", err)
	}
	gitEnvironment := map[string]string{
		"GIT_CONFIG_GLOBAL":   "/dev/null",
		"GIT_CONFIG_NOSYSTEM": "1",
		"HOME":                cwd,
		"PATH":                os.Getenv("PATH"),
	}
	code, _, stderr := runWorkspaceCommand(t, []string{git, "init", "--quiet"}, cwd, gitEnvironment, nil)
	if code != 0 {
		t.Fatalf("git init as agent failed: %s", stderr)
	}
	assertOwner(t, filepath.Join(cwd, ".git"), uid, gid)
	code, stdout, stderr := runWorkspaceCommand(t, []string{git, "rev-parse", "--show-toplevel"}, cwd, gitEnvironment, nil)
	if code != 0 || strings.TrimSpace(stdout) != cwd {
		t.Fatalf("git rev-parse as agent = (%d, %q, %q), want %q", code, stdout, stderr, cwd)
	}

	existing := filepath.Join(root, "authored")
	if err := os.Mkdir(existing, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(existing, "notes.txt"), []byte("authored data"), 0o600); err != nil {
		t.Fatal(err)
	}
	existingOwner := ownerOf(t, existing)
	fileOwner := ownerOf(t, filepath.Join(existing, "notes.txt"))
	resolved, err := EnsureWorkspace(context.Background(), existing, root, nil)
	if err != nil {
		t.Fatal(err)
	}
	if resolved != existing || ownerOf(t, existing) != existingOwner || ownerOf(t, filepath.Join(existing, "notes.txt")) != fileOwner {
		t.Fatal("EnsureWorkspace changed an existing workspace or authored file ownership")
	}
	content, err := os.ReadFile(filepath.Join(existing, "notes.txt"))
	if err != nil || string(content) != "authored data" {
		t.Fatalf("existing workspace contents = %q, %v", content, err)
	}

	parent := filepath.Join(root, "existing-parent")
	if err := os.Mkdir(parent, 0o777); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(parent, 0o777); err != nil {
		t.Fatal(err)
	}
	parentOwner := ownerOf(t, parent)
	nested := filepath.Join(parent, "new-session", "nested")
	if _, err := EnsureWorkspace(context.Background(), nested, root, nil); err != nil {
		t.Fatal(err)
	}
	if ownerOf(t, parent) != parentOwner {
		t.Fatal("EnsureWorkspace changed the existing parent directory owner")
	}
	assertOwner(t, filepath.Join(parent, "new-session"), uid, gid)
	assertOwner(t, nested, uid, gid)
}

func TestEnsureWorkspaceRejectsSymlinkEscapeBeforeCreatingAnything(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(t.TempDir(), "new")
	if err := os.Symlink(filepath.Dir(outside), filepath.Join(root, "escape")); err != nil {
		t.Fatal(err)
	}
	if _, err := EnsureWorkspace(context.Background(), filepath.Join(root, "escape", "new"), root, nil); err == nil || !strings.Contains(err.Error(), "must be a child") {
		t.Fatalf("EnsureWorkspace symlink escape error = %v", err)
	}
	if _, err := os.Stat(outside); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("symlink escape created an outside directory: stat error %v", err)
	}
}

func TestEnsureWorkspaceWithoutIsolationKeepsRunnerIdentity(t *testing.T) {
	cwd := filepath.Join(t.TempDir(), "pod", "new-session")
	created, err := EnsureWorkspace(context.Background(), cwd, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if created != cwd || ownerOf(t, cwd) != (fileOwner{uid: uint32(os.Geteuid()), gid: uint32(os.Getegid())}) {
		t.Fatalf("rootless workspace = %q owner %+v", created, ownerOf(t, cwd))
	}
}

type fileOwner struct {
	uid uint32
	gid uint32
}

func ownerOf(t *testing.T, path string) fileOwner {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		t.Fatalf("stat %s returned unexpected metadata %T", path, info.Sys())
	}
	return fileOwner{uid: stat.Uid, gid: stat.Gid}
}

func assertOwner(t *testing.T, path string, uid, gid uint32) {
	t.Helper()
	if got := ownerOf(t, path); got != (fileOwner{uid: uid, gid: gid}) {
		t.Fatalf("owner of %s = %+v, want uid=%d gid=%d", path, got, uid, gid)
	}
}

func runWorkspaceCommand(t *testing.T, command []string, cwd string, environment map[string]string, policy *VMProcessPolicy) (int, string, string) {
	t.Helper()
	process, err := NewProcess(ProcessConfig{Command: command, Dir: cwd, Env: environment, VMProcessPolicy: policy})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := process.CloseStdin(); err != nil {
		_ = process.Stop(context.Background(), time.Second)
		t.Fatal(err)
	}
	code, err := process.Wait(ctx)
	if err != nil {
		_ = process.Stop(context.Background(), time.Second)
		t.Fatal(err)
	}
	stdout, stdoutErr := io.ReadAll(process.Stdout())
	stderr, stderrErr := io.ReadAll(process.Stderr())
	if stdoutErr != nil || stderrErr != nil {
		t.Fatalf("read native command output: stdout %v, stderr %v", stdoutErr, stderrErr)
	}
	return code, string(bytes.TrimSpace(stdout)), string(bytes.TrimSpace(stderr))
}
