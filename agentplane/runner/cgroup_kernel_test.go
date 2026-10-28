package runner

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"go.uber.org/zap"
	"golang.org/x/sys/unix"
)

func TestMain(m *testing.M) {
	logger, err := zap.NewProduction()
	if err != nil {
		fmt.Fprintf(os.Stderr, "cgroup test logging setup failed: %v\n", err)
		os.Exit(1)
	}
	zap.ReplaceGlobals(logger)
	if handled, code := InternalMode(os.Args[1:]); handled {
		_ = logger.Sync()
		os.Exit(code)
	}
	code := m.Run()
	_ = logger.Sync()
	os.Exit(code)
}

// TestCgroupKernelFixture runs in the privileged, private cgroup namespace owned
// by test_cgroups.py.
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
	groups := &AgentCgroups{
		Root: filepath.Join(mount, "workloads"), AgentUID: 107, AgentGID: 107, BlockDevices: []string{device},
		AggregateLimits: ResourceLimits{CPUQuotaMicros: 100000, MemoryMaxBytes: 1140850688, PidsMax: 768, ReadBPS: 52428800, WriteBPS: 20971520, ReadIOPS: 1000, WriteIOPS: 500},
		ProcessLimits:   ResourceLimits{CPUQuotaMicros: 100000, MemoryMaxBytes: 805306368, PidsMax: 512, ReadBPS: 52428800, WriteBPS: 20971520, ReadIOPS: 1000, WriteIOPS: 500},
	}
	if err := groups.Prepare(); err != nil {
		t.Fatal(err)
	}
	group, err := groups.CreateProcessGroup(nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, limits := range []struct {
		path, cpu, memory, pids, oomGroup string
	}{
		{groups.Root, "100000 100000", "1140850688", "768", "0"},
		{group, "100000 100000", "805306368", "512", "0"},
	} {
		for name, want := range map[string]string{
			"cpu.max": limits.cpu, "memory.max": limits.memory, "pids.max": limits.pids,
			"memory.swap.max": "0", "memory.oom.group": limits.oomGroup,
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
	limits := &ResourceLimits{
		CPUQuotaMicros: 100000, MemoryMaxBytes: 64 * 1024 * 1024, PidsMax: 32,
		ReadBPS: 52428800, WriteBPS: 20971520, ReadIOPS: 1000, WriteIOPS: 500,
	}
	assertToolOOMPreservesNativeLeader(t, groups, limits, ctx)
	assertProcessOOMIsolation(t, groups, limits, ctx)
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

func assertToolOOMPreservesNativeLeader(t *testing.T, groups *AgentCgroups, limits *ResourceLimits, ctx context.Context) {
	t.Helper()
	process, err := NewProcess(ProcessConfig{
		Command: []string{"/bin/sh", "-c", `
/bin/sleep 1000 &
peer=$!
"$1" -test.run='^TestCgroupOOMFixture$'
result=$?
printf 'tool-oom-survived=%d peer=%d\n' "$result" "$peer"
read -r confirmation
`, "tool-oom-fixture", os.Args[0]},
		Dir: "/tmp", Env: map[string]string{"AGENTPLANE_CGROUP_OOM_FIXTURE": "1"},
		ProcessIsolation: groups, ResourceLimits: limits,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = process.Stop(context.Background(), stopGrace) }()
	go func() { _, _ = io.Copy(io.Discard, process.Stderr()) }()
	stdout := bufio.NewReader(process.Stdout())
	line, err := stdout.ReadString('\n')
	var descendant int
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fmt.Sscanf(line, "oom-descendant=%d", &descendant); err != nil || descendant <= 0 {
		t.Fatalf("tool did not start its descendant: %q, %v", line, err)
	}
	line, err = stdout.ReadString('\n')
	var code, peer int
	if err != nil {
		t.Fatalf("native parent did not survive tool OOM: %v; %s", err, process.ExitDescription())
	}
	if _, err := fmt.Sscanf(line, "tool-oom-survived=%d peer=%d", &code, &peer); err != nil || code != 137 || peer <= 0 {
		t.Fatalf("tool exit = %q, %v; want SIGKILL (137) and surviving peer", line, err)
	}
	assertExecuting(t, process.PID(), "native parent after tool OOM")
	assertExecuting(t, peer, "same-leaf peer after tool OOM")
	assertExecuting(t, descendant, "tool descendant before leader cleanup")
	if err := process.WriteLine(ctx, "finish"); err != nil {
		t.Fatal(err)
	}
	if code, err := process.Wait(ctx); err != nil || code != 0 {
		t.Fatalf("native parent exit = %d, %v; %s", code, err, process.ExitDescription())
	}
	assertNotExecuting(t, peer, "same-leaf peer after leader cleanup")
	assertNotExecuting(t, descendant, "tool descendant after leader cleanup")
}

func assertProcessOOMIsolation(t *testing.T, groups *AgentCgroups, limits *ResourceLimits, ctx context.Context) {
	t.Helper()
	sibling, err := NewProcess(ProcessConfig{Command: []string{"/bin/sleep", "1000"}, Dir: "/tmp", Env: map[string]string{}, ProcessIsolation: groups})
	if err != nil {
		t.Fatal(err)
	}
	if err := sibling.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sibling.Stop(context.Background(), stopGrace) }()
	go func() { _, _ = io.Copy(io.Discard, sibling.Stderr()) }()
	siblingPID := sibling.PID()

	process, err := NewProcess(ProcessConfig{
		Command: []string{os.Args[0], "-test.run=^TestCgroupOOMFixture$"},
		Dir:     "/tmp", Env: map[string]string{"AGENTPLANE_CGROUP_OOM_FIXTURE": "1"},
		ProcessIsolation: groups, ResourceLimits: limits,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := process.Start(ctx); err != nil {
		t.Fatal(err)
	}
	stderr := make(chan []byte, 1)
	go func() {
		data, _ := io.ReadAll(process.Stderr())
		stderr <- data
	}()
	line, err := bufio.NewReader(process.Stdout()).ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	var descendant int
	if _, err := fmt.Sscanf(line, "oom-descendant=%d", &descendant); err != nil || descendant <= 0 {
		t.Fatalf("OOM fixture did not start its descendant: %q, %v", line, err)
	}
	code, err := process.Wait(ctx)
	if err != nil || code != 137 {
		t.Fatalf("OOM fixture exit = %d, %v; want SIGKILL (137)", code, err)
	}
	logLines := <-stderr
	var diagnostics struct {
		Message         string            `json:"msg"`
		MemoryEvents    map[string]uint64 `json:"memory_events"`
		MemoryPeakBytes uint64            `json:"memory_peak_bytes"`
		MemoryPeakError string            `json:"memory_peak_error"`
	}
	found := false
	for _, logLine := range strings.Split(string(logLines), "\n") {
		var candidate struct {
			Message         string            `json:"msg"`
			MemoryEvents    map[string]uint64 `json:"memory_events"`
			MemoryPeakBytes uint64            `json:"memory_peak_bytes"`
			MemoryPeakError string            `json:"memory_peak_error"`
		}
		if json.Unmarshal([]byte(logLine), &candidate) == nil && candidate.Message == "agent process cgroup memory diagnostics" {
			diagnostics = candidate
			found = true
			break
		}
	}
	if !found || diagnostics.MemoryEvents["oom"] == 0 || diagnostics.MemoryEvents["oom_kill"] == 0 ||
		(diagnostics.MemoryPeakBytes == 0 && diagnostics.MemoryPeakError == "") {
		t.Fatalf("OOM diagnostics missing from supervisor logs: found=%v diagnostics=%+v stderr=%s", found, diagnostics, logLines)
	}
	assertNotExecuting(t, descendant, "descendant after OOM-killed leader cleanup")
	if !sibling.Running() {
		t.Fatal("OOM in one process cgroup stopped its sibling process")
	}
	assertExecuting(t, siblingPID, "sibling process")
}

func assertNotExecuting(t *testing.T, pid int, description string) {
	t.Helper()
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if os.IsNotExist(err) {
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	_, state, _ := strings.Cut(string(data), ") ")
	if !strings.HasPrefix(state, "Z ") && !strings.HasPrefix(state, "X ") {
		t.Fatalf("%s survived its cgroup kill: %s", description, data)
	}
}

func assertExecuting(t *testing.T, pid int, description string) {
	t.Helper()
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		t.Fatalf("%s disappeared: %v", description, err)
	}
	_, state, _ := strings.Cut(string(data), ") ")
	if strings.HasPrefix(state, "Z ") || strings.HasPrefix(state, "X ") {
		t.Fatalf("%s is no longer executing: %s", description, data)
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

func TestCgroupOOMFixture(t *testing.T) {
	if os.Getenv("AGENTPLANE_CGROUP_OOM_FIXTURE") != "1" {
		return
	}
	child := exec.Command("/bin/sleep", "1000")
	child.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	fmt.Printf("oom-descendant=%d\n", child.Process.Pid)
	// Touch every page so the kernel must charge this allocation to the small process leaf.
	allocation := make([]byte, 256*1024*1024)
	for offset := 0; offset < len(allocation); offset += os.Getpagesize() {
		allocation[offset] = 1
	}
	runtime.KeepAlive(allocation)
	t.Fatal("large allocation survived the configured cgroup memory max")
}
