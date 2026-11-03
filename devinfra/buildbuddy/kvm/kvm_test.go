package kvm

import (
	"os"
	"strings"
	"syscall"
	"testing"
)

const (
	// KVM ioctl numbers use _IO(KVMIO, n) from linux/kvm.h.
	kvmGetAPIVersion = 0xAE00
	kvmCreateVM      = 0xAE01
	kvmCreateVCPU    = 0xAE41
	kvmAPIVersion    = 12
)

func TestKVMCanCreateVMAndVCPU(t *testing.T) {
	kvm, err := os.OpenFile("/dev/kvm", os.O_RDWR, 0)
	if err != nil {
		t.Fatalf("open /dev/kvm: %v", err)
	}
	closeOnCleanup(t, kvm)

	version := ioctl(t, kvm, kvmGetAPIVersion, 0)
	if version != kvmAPIVersion {
		t.Fatalf("KVM API version = %d, want %d", version, kvmAPIVersion)
	}
	t.Logf("KVM API version: %d", version)
	logEffectiveCapabilities(t)

	vm := fileFromIoctl(t, ioctl(t, kvm, kvmCreateVM, 0), "KVM VM")
	t.Log("created KVM VM")
	fileFromIoctl(t, ioctl(t, vm, kvmCreateVCPU, 0), "KVM vCPU")
	t.Log("created KVM vCPU")
}

func logEffectiveCapabilities(t *testing.T) {
	t.Helper()
	status, err := os.ReadFile("/proc/self/status")
	if err != nil {
		t.Fatalf("read /proc/self/status: %v", err)
	}
	for _, line := range strings.Split(string(status), "\n") {
		if strings.HasPrefix(line, "CapEff:") {
			t.Logf("/proc/self/status %s", line)
			return
		}
	}
	t.Fatal("/proc/self/status has no CapEff field")
}

func ioctl(t *testing.T, file *os.File, request uintptr, arg uintptr) uintptr {
	t.Helper()
	result, _, errno := syscall.Syscall(syscall.SYS_IOCTL, file.Fd(), request, arg)
	if errno != 0 {
		t.Fatalf("ioctl %#x on %s: %v", request, file.Name(), errno)
	}
	return result
}

func fileFromIoctl(t *testing.T, fd uintptr, name string) *os.File {
	t.Helper()
	file := os.NewFile(fd, name)
	if file == nil {
		if err := syscall.Close(int(fd)); err != nil {
			t.Errorf("close %s descriptor %d after wrapping it failed: %v", name, fd, err)
		}
		t.Fatalf("ioctl returned invalid %s descriptor %d", name, fd)
	}
	closeOnCleanup(t, file)
	return file
}

func closeOnCleanup(t *testing.T, file *os.File) {
	t.Helper()
	t.Cleanup(func() {
		if err := file.Close(); err != nil {
			t.Errorf("close %s: %v", file.Name(), err)
		}
	})
}
