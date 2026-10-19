package runner

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// TestCgroupKernelFixture runs in the privileged, private cgroup namespace owned
// by test_cgroups.py. The ordinary Go suite only registers this fixture.
func TestCgroupKernelFixture(t *testing.T) {
	if os.Getenv("AGENTPLANE_CGROUP_KERNEL_FIXTURE") != "1" {
		return
	}
	// Leave the delegation root empty so controllers can be enabled there.
	const mount = "/sys/fs/cgroup"
	if err := os.Mkdir(filepath.Join(mount, "runtime"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(mount, "runtime/cgroup.procs"), []byte(strconv.Itoa(os.Getpid())), 0); err != nil {
		t.Fatal(err)
	}
	devices, err := filepath.Glob("/sys/block/*/dev")
	if err != nil {
		t.Fatal(err)
	}
	var major, minor uint32
	for _, path := range devices {
		name := filepath.Base(filepath.Dir(path))
		if strings.HasPrefix(name, "loop") || strings.HasPrefix(name, "ram") {
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := fmt.Sscanf(string(data), "%d:%d", &major, &minor); err != nil {
			t.Fatal(err)
		}
		break
	}
	if major == 0 {
		t.Fatal("no block device for the kernel I/O throttle test")
	}
	device := filepath.Join(t.TempDir(), "state-device")
	if err := unix.Mknod(device, unix.S_IFBLK|0o600, int(unix.Mkdev(major, minor))); err != nil {
		t.Fatal(err)
	}
	groups := &AgentCgroups{Root: filepath.Join(mount, "workloads"), AgentUID: 107, AgentGID: 107, BlockDevices: []string{device}}
	if err := groups.Prepare(); err != nil {
		t.Fatal(err)
	}
	group, err := groups.CreateProcessGroup()
	if err != nil {
		t.Fatal(err)
	}
	for _, limits := range []struct {
		path, cpu, memory, pids string
	}{
		{groups.Root, "400000 100000", "4294967296", "768"},
		{group, "300000 100000", "3221225472", "512"},
	} {
		for name, want := range map[string]string{
			"cpu.max": limits.cpu, "memory.max": limits.memory, "pids.max": limits.pids,
			"memory.swap.max": "0", "memory.oom.group": "1",
		} {
			data, err := os.ReadFile(filepath.Join(limits.path, name))
			if err != nil || strings.TrimSpace(string(data)) != want {
				t.Fatalf("%s/%s = %q, %v; want %q", limits.path, name, data, err, want)
			}
		}
		data, err := os.ReadFile(filepath.Join(limits.path, "io.max"))
		if err != nil {
			t.Fatal(err)
		}
		fields := strings.Fields(string(data))
		if len(fields) != 5 || fields[0] != fmt.Sprintf("%d:%d", major, minor) {
			t.Fatalf("unexpected device limits: %s", data)
		}
		for _, field := range []string{"rbps=52428800", "wbps=20971520", "riops=1000", "wiops=500"} {
			if !strings.Contains(" "+string(data), " "+field) {
				t.Fatalf("io.max lost %s: %s", field, data)
			}
		}
	}
	// Startup also removes groups left by a previous runner.
	if err := groups.Prepare(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(group); !os.IsNotExist(err) {
		t.Fatalf("stale process group survived preparation: %v", err)
	}

	process, err := NewProcess(ProcessConfig{
		Command: []string{os.Args[0], "-test.run=^TestCgroupNativeFixture$"},
		Dir:     "/tmp", Env: map[string]string{"AGENTPLANE_CGROUP_NATIVE_FIXTURE": "1"},
		ProcessIsolation: groups,
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := process.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = process.Stop(context.Background(), stopGrace) }()
	go func() { _, _ = io.Copy(io.Discard, process.Stderr()) }()
	line, err := bufio.NewReader(process.Stdout()).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	var descendant int
	if _, err := fmt.Sscanf(line, "detached=%d", &descendant); err != nil || descendant <= 0 {
		t.Fatalf("native child did not start: %q, %v", line, err)
	}
	if code, err := process.Wait(ctx); err != nil || code != 0 {
		t.Fatalf("native exit: %d, %v; %s", code, err, process.ExitDescription())
	}
	if stat, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", descendant)); err == nil {
		// An orphan may await PID 1's reaper, but it must no longer execute.
		_, state, _ := strings.Cut(string(stat), ") ")
		if !strings.HasPrefix(state, "Z ") {
			t.Fatalf("detached descendant survived cgroup fencing: %s", stat)
		}
	} else if !os.IsNotExist(err) {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(groups.Root)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.IsDir() {
			t.Fatalf("process cgroup survived supervisor cleanup: %s", entry.Name())
		}
	}
}

func TestCgroupNativeFixture(t *testing.T) {
	if os.Getenv("AGENTPLANE_CGROUP_NATIVE_FIXTURE") != "1" {
		return
	}
	if os.Getuid() != 107 || os.Getgid() != 107 {
		t.Fatalf("native identity = %d:%d", os.Getuid(), os.Getgid())
	}
	membership, err := os.ReadFile("/proc/self/cgroup")
	if err != nil || !strings.Contains(string(membership), "/workloads/process-") {
		t.Fatalf("native process escaped its limited group: %s, %v", membership, err)
	}
	child := exec.Command("/bin/sleep", "1000")
	child.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	fmt.Printf("detached=%d\n", child.Process.Pid)
	// Exit the leader while a descendant lives outside its process group. The
	// supervisor must fence that descendant using the cgroup before it exits.
}
