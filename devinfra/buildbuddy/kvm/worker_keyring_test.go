package kvm

import (
	"os"
	"os/exec"
	"syscall"
	"testing"
)

func TestNestedRuncCanJoinSessionKeyring(t *testing.T) {
	if os.Getenv("KVM_KEYRING_TEST_CHILD") == "1" {
		// KEYCTL_JOIN_SESSION_KEYRING with a null name creates an anonymous keyring,
		// as runc does. Use a child process so changing its keyring has no effect on
		// any of the test runner's threads.
		_, _, errno := syscall.Syscall6(syscall.SYS_KEYCTL, 1, 0, 0, 0, 0, 0)
		if errno != 0 {
			t.Fatalf("join session keyring: %v", errno)
		}
		// KEYCTL_READ remains blocked by the outer Docker profile.
		_, _, errno = syscall.Syscall6(syscall.SYS_KEYCTL, 11, 0, 0, 0, 0, 0)
		if errno != syscall.EPERM {
			t.Fatalf("keyring read: got %v, want EPERM", errno)
		}
		return
	}
	cmd := exec.Command(os.Args[0], "-test.run=^TestNestedRuncCanJoinSessionKeyring$")
	cmd.Env = append(os.Environ(), "KVM_KEYRING_TEST_CHILD=1")
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("keyring probe failed: %v\n%s", err, output)
	}
}
