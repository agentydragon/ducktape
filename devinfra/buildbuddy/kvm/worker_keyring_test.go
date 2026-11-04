package kvm

import (
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"unsafe"
)

func TestNestedRuncCanJoinSessionKeyring(t *testing.T) {
	if os.Getenv("KVM_KEYRING_TEST_CHILD") == "1" {
		// Mirror runc's named keyring creation and permission setup. A child
		// process keeps its session keyring separate from the test runner.
		name, err := syscall.BytePtrFromString(fmt.Sprintf("_ses.kvm-test-%d", os.Getpid()))
		if err != nil {
			t.Fatal(err)
		}
		id, _, errno := syscall.Syscall6(syscall.SYS_KEYCTL, 1, uintptr(unsafe.Pointer(name)), 0, 0, 0, 0)
		if errno != 0 {
			t.Fatalf("join session keyring: %v", errno)
		}
		buf := make([]byte, 1024)
		n, _, errno := syscall.Syscall6(syscall.SYS_KEYCTL, 6, id, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), 0, 0)
		if errno != 0 || n >= uintptr(len(buf)) {
			t.Fatalf("describe session keyring: bytes=%d error=%v", n, errno)
		}
		parts := strings.Split(string(buf[:n]), ";")
		if len(parts) < 5 {
			t.Fatalf("unexpected keyring description: %q", buf[:n])
		}
		perms, err := strconv.ParseUint(parts[3], 16, 32)
		if err != nil {
			t.Fatalf("parse keyring permissions: %v", err)
		}
		_, _, errno = syscall.Syscall6(syscall.SYS_KEYCTL, 5, id, uintptr(perms|0x80000), 0, 0, 0)
		if errno != 0 {
			t.Fatalf("set session keyring permissions: %v", errno)
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
