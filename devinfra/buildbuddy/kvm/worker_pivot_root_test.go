package kvm

import (
	"errors"
	"path/filepath"
	"syscall"
	"testing"
)

func TestNestedRuncCanCallPivotRoot(t *testing.T) {
	// Nonexistent paths exercise the outer seccomp filter without switching roots.
	// A permitted syscall reaches pathname lookup; a blocked one returns EPERM.
	root := filepath.Join(t.TempDir(), "absent")
	if err := syscall.PivotRoot(root, filepath.Join(root, "old")); !errors.Is(err, syscall.ENOENT) {
		t.Fatalf("pivot_root permission probe: got %v, want ENOENT", err)
	}
}
