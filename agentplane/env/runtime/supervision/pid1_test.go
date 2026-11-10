package supervision

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

func TestNamespaceWorkloadPIDsHandlesExitedProcessWithoutWeakeningCredentialFence(t *testing.T) {
	procDir := t.TempDir()
	for _, pid := range []string{"1", "17", "18"} {
		if err := os.Mkdir(filepath.Join(procDir, pid), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	status := func(path string) ([]byte, error) {
		switch filepath.Base(filepath.Dir(path)) {
		case "17":
			return nil, unix.ESRCH // exited after /proc was listed
		case "18":
			return []byte("Name:\tworker\nUid:\t1000\t1000\t1000\t1000\n"), nil
		default:
			t.Fatalf("unexpected status read: %s", path)
			return nil, nil
		}
	}
	pids, err := namespaceWorkloadPIDsAt(procDir, 1000, status)
	if err != nil || !slices.Equal(pids, []int{18}) {
		t.Fatalf("namespace workloads = %v, %v; want only live PID 18", pids, err)
	}

	for _, test := range []struct {
		name   string
		status []byte
		err    error
	}{
		{name: "read denied", err: os.ErrPermission},
		{name: "unexpected UID", status: []byte("Uid:\t2000\t2000\t2000\t2000\n")},
		{name: "malformed UID", status: []byte("Uid:\tbroken\n")},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := namespaceWorkloadPIDsAt(procDir, 1000, func(path string) ([]byte, error) {
				if filepath.Base(filepath.Dir(path)) == "17" {
					return nil, unix.ESRCH
				}
				return test.status, test.err
			})
			if err == nil || errors.Is(err, unix.ESRCH) {
				t.Fatalf("unexpected credential error = %v; want fail-closed error", err)
			}
		})
	}
}

func TestPID1BackendRejectsNonPID1(t *testing.T) {
	if _, err := NewPID1Supervisor(); err == nil || !strings.Contains(err.Error(), "requires Runner to be PID 1") {
		t.Fatalf("PID1 backend accepted a non-PID1 process: %v", err)
	}
}
