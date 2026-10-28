package runner

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

// ResolveWorkspace resolves cwd and rejects the workspace root itself or a path outside it.
// An empty workspaceRoot leaves cwd as provided, matching non-mounted local workspaces.
func ResolveWorkspace(cwd, workspaceRoot string) (string, error) {
	if workspaceRoot == "" {
		return cwd, nil
	}
	root, err := resolveWithMissingTail(workspaceRoot)
	if err != nil {
		return "", err
	}
	candidate, err := resolveWithMissingTail(cwd)
	if err != nil {
		return "", err
	}
	relative, err := filepath.Rel(root, candidate)
	if err != nil || relative == "." || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("spec.cwd must be a child of %s, not %q", root, cwd)
	}
	return candidate, nil
}

// EnsureWorkspace returns cwd after creating any missing path components. Existing
// directories and their contents retain their original ownership.
func EnsureWorkspace(ctx context.Context, cwd, workspaceRoot string, policy *VMProcessPolicy) (string, error) {
	resolved, err := ResolveWorkspace(cwd, workspaceRoot)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(resolved)
	if err == nil {
		if !info.IsDir() {
			return "", fmt.Errorf("workspace path is not a directory: %s", resolved)
		}
		return resolved, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("inspect workspace path %s: %w", resolved, err)
	}
	if policy == nil {
		if workspaceRoot == "" {
			if err := os.MkdirAll(resolved, 0o770); err != nil {
				return "", fmt.Errorf("create workspace path %s: %w", resolved, err)
			}
		} else {
			root, err := resolveWithMissingTail(workspaceRoot)
			if err != nil {
				return "", err
			}
			relative, err := filepath.Rel(root, resolved)
			if err != nil {
				return "", err
			}
			if err := createBeneath(root, strings.Split(relative, string(filepath.Separator))); err != nil {
				return "", fmt.Errorf("create workspace below %s: %w", root, err)
			}
		}
		return ResolveWorkspace(resolved, workspaceRoot)
	}
	if workspaceRoot == "" {
		return "", errors.New("an isolated harness workspace requires a configured workspace root")
	}
	root, err := resolveWithMissingTail(workspaceRoot)
	if err != nil {
		return "", err
	}
	relative, err := filepath.Rel(root, resolved)
	if err != nil || relative == "." || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("spec.cwd must be a child of %s, not %q", root, cwd)
	}
	parts := strings.Split(relative, string(filepath.Separator))
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("locate runner executable for workspace creation: %w", err)
	}
	command := append([]string{executable, workspaceMode, root}, parts...)
	process, err := NewProcess(ProcessConfig{
		Command:         command,
		VMProcessPolicy: policy,
		Dir:             root,
		Env:             map[string]string{},
	})
	if err != nil {
		return "", err
	}
	if err := process.Start(ctx); err != nil {
		return "", err
	}
	stdoutDone := make(chan struct{})
	stderrDone := make(chan struct{})
	go func() {
		_, _ = io.Copy(io.Discard, process.Stdout())
		close(stdoutDone)
	}()
	go func() {
		_, _ = io.Copy(io.Discard, process.Stderr())
		close(stderrDone)
	}()
	if err := process.CloseStdin(); err != nil {
		_ = process.Stop(context.Background(), stopGrace)
		return "", err
	}
	code, waitErr := process.Wait(ctx)
	if waitErr != nil {
		_ = process.Stop(context.Background(), stopGrace)
		return "", waitErr
	}
	<-stdoutDone
	<-stderrDone
	if code != 0 {
		return "", fmt.Errorf("harness workspace creation failed: %s", process.ExitDescription())
	}
	return ResolveWorkspace(resolved, workspaceRoot)
}

// WorkspaceMain creates the requested path under a root descriptor without following symlinks.
func WorkspaceMain(args []string) int {
	if len(args) < 2 {
		fmt.Fprintln(os.Stderr, "workspace creator requires ROOT COMPONENT [COMPONENT ...]")
		return 64
	}
	if err := createBeneath(args[0], args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "workspace creator: %v\n", err)
		return 1
	}
	return 0
}

func createBeneath(root string, parts []string) error {
	descriptor, err := unix.Open(root, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
	if err != nil {
		return fmt.Errorf("open workspace root %s: %w", root, err)
	}
	defer func() { _ = unix.Close(descriptor) }()
	for _, part := range parts {
		if part == "" || part == "." || part == ".." || strings.Contains(part, string(filepath.Separator)) {
			return fmt.Errorf("invalid workspace path component %q", part)
		}
		if err := unix.Mkdirat(descriptor, part, 0o770); err != nil && err != unix.EEXIST {
			return fmt.Errorf("create workspace component %s: %w", part, err)
		}
		child, err := unix.Openat(descriptor, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
		if err != nil {
			return fmt.Errorf("open workspace component %s without following symlinks: %w", part, err)
		}
		_ = unix.Close(descriptor)
		descriptor = child
	}
	return nil
}

func resolveWithMissingTail(path string) (string, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	current := filepath.Clean(absolute)
	var suffix []string
	for {
		_, statErr := os.Lstat(current)
		if statErr == nil {
			resolved, evalErr := filepath.EvalSymlinks(current)
			if evalErr != nil {
				return "", fmt.Errorf("resolve workspace path %s: %w", path, evalErr)
			}
			for i := len(suffix) - 1; i >= 0; i-- {
				resolved = filepath.Join(resolved, suffix[i])
			}
			return filepath.Clean(resolved), nil
		}
		if !errors.Is(statErr, os.ErrNotExist) {
			return "", fmt.Errorf("resolve workspace path %s: %w", path, statErr)
		}
		parent := filepath.Dir(current)
		if parent == current {
			return "", fmt.Errorf("resolve workspace path %s: no existing ancestor", path)
		}
		suffix = append(suffix, filepath.Base(current))
		current = parent
	}
}
