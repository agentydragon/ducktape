package qemu_tests

import (
	"bufio"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/big"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/bazelbuild/rules_go/go/runfiles"
)

// Runfile paths for shared test artifacts.
const (
	VmlinuzPath        = "cluster/kubespand/qemu_tests/vmlinuz-virt"
	DiscoveryInitramfs = "cluster/kubespand/qemu_tests/vms/discovery/initramfs.cpio.gz"
	RouterInitramfs    = "cluster/kubespand/qemu_tests/vms/router/initramfs.cpio.gz"
	KubespanInitramfs  = "cluster/kubespand/qemu_tests/vms/kubespan/initramfs.cpio.gz"
	DoublenatInitramfs = "cluster/kubespand/qemu_tests/vms/doublenat/initramfs.cpio.gz"
	NftInitramfs       = "cluster/kubespand/qemu_tests/nft/initramfs.cpio.gz"
	// Talos artifacts are external repos — no _main/ prefix in Rlocation.
	TalosNocloudQcow2Path = "talos_nocloud_amd64/file/nocloud-amd64.qcow2.xz"
	TalosctlPath          = "talosctl_amd64/file/talosctl"

	// Pre-generated Talos configs (committed as testdata).
	TalosVPSConfig  = "cluster/kubespand/qemu_tests/talos/testdata/vps/controlplane.yaml"
	TalosNAT1Config = "cluster/kubespand/qemu_tests/talos/testdata/nat1/worker.yaml"
	TalosNAT2Config = "cluster/kubespand/qemu_tests/talos/testdata/nat2/worker.yaml"
	TalosConfig     = "cluster/kubespand/qemu_tests/talos/testdata/vps/talosconfig"
)

// VM represents a running QEMU VM.
type VM struct {
	Name   string
	Events chan Event // buffered channel; receives all parsed events
	Done   chan struct{}
	cmd    *exec.Cmd
	events []Event
	rawLog strings.Builder
	mu     sync.Mutex
}

func (v *VM) Wait() {
	<-v.Done
}

func (v *VM) Kill() {
	if v.cmd != nil && v.cmd.Process != nil {
		v.cmd.Process.Kill()
	}
}

func (v *VM) GetEvents() []Event {
	v.mu.Lock()
	defer v.mu.Unlock()
	return append([]Event{}, v.events...)
}

func (v *VM) GetRawLog() string {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.rawLog.String()
}

// SaveLogs saves raw log and event artifacts for this VM using its name.
func (v *VM) SaveLogs(t *testing.T, dir string) {
	t.Helper()
	SaveArtifact(t, dir, v.Name+".log", v.GetRawLog())
	SaveEventsArtifact(t, dir, v.Name+"-events.jsonl", v.GetEvents())
}

// BootVM starts a QEMU VM with the given kernel cmdline args.
func BootVM(t *testing.T, name string, vmlinuz, initramfs string, kernelArgs string, extraQemuArgs ...string) *VM {
	t.Helper()

	args := []string{
		"-kernel", vmlinuz,
		"-initrd", initramfs,
		"-append", "console=ttyS0 panic=-1 quiet " + kernelArgs,
		"-nographic",
		"-no-reboot",
		"-m", "1024",
		"-machine", "accel=tcg",
		"-cpu", "max",
		"-display", "none",
	}
	args = append(args, extraQemuArgs...)

	return StartVM(t, name, exec.Command("qemu-system-x86_64", args...), true)
}

// StartVM starts a QEMU process from a pre-built command and returns a VM.
// Use this for custom QEMU configurations (e.g., Talos VMs with CIDATA drives).
// If parseEvents is true, JSON event lines are parsed; otherwise only raw log is captured.
func StartVM(t *testing.T, name string, cmd *exec.Cmd, parseEvents bool) *VM {
	t.Helper()

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("stdout pipe: %v", err)
	}
	cmd.Stderr = cmd.Stdout

	v := &VM{
		Name:   name,
		Events: make(chan Event, 64),
		Done:   make(chan struct{}),
		cmd:    cmd,
	}

	if err := cmd.Start(); err != nil {
		t.Fatalf("start QEMU %s: %v", name, err)
	}

	go func() {
		defer close(v.Done)
		defer close(v.Events)
		scanner := bufio.NewScanner(stdout)
		if !parseEvents {
			scanner.Buffer(make([]byte, 0, 256*1024), 256*1024)
		}
		for scanner.Scan() {
			line := scanner.Text()
			v.mu.Lock()
			v.rawLog.WriteString(line)
			v.rawLog.WriteByte('\n')
			v.mu.Unlock()

			if parseEvents {
				var evt Event
				if json.Unmarshal([]byte(line), &evt) == nil && evt.Type != "" {
					v.mu.Lock()
					v.events = append(v.events, evt)
					v.mu.Unlock()
					t.Logf("[%s] %s: %s", name, evt.Type, evt.Message)
					v.Events <- evt
				}
			}
		}
		cmd.Wait()
	}()

	return v
}

// McastNIC returns QEMU args for a virtio-net NIC on a multicast socket bridge.
func McastNIC(id, mcastAddr, mac string) []string {
	return []string{
		"-netdev", fmt.Sprintf("socket,id=%s,mcast=%s", id, mcastAddr),
		"-device", fmt.Sprintf("virtio-net-pci,netdev=%s,mac=%s", id, mac),
	}
}

// KillAndWait kills all VMs and waits for them to exit.
func KillAndWait(vms ...*VM) {
	for _, v := range vms {
		v.Kill()
	}
	for _, v := range vms {
		<-v.Done
	}
}

func SaveArtifact(t *testing.T, dir, name, content string) {
	t.Helper()
	path := filepath.Join(dir, name)
	os.WriteFile(path, []byte(content), 0o644)
}

func SaveEventsArtifact(t *testing.T, dir, name string, events []Event) {
	t.Helper()
	var sb strings.Builder
	for _, e := range events {
		b, _ := json.Marshal(e)
		sb.Write(b)
		sb.WriteByte('\n')
	}
	SaveArtifact(t, dir, name, sb.String())
}

func RandomBase64(n int) string {
	buf := make([]byte, n)
	rand.Read(buf)
	return base64.StdEncoding.EncodeToString(buf)
}

func RandomPort() int {
	n, _ := rand.Int(rand.Reader, big.NewInt(50000))
	return 10000 + int(n.Int64())
}

// WaitForEvent blocks until the VM emits an event of the given type.
// Returns immediately on VM exit or timeout.
func (v *VM) WaitForEvent(typ EventType, timeout time.Duration) (Event, bool) {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	for {
		select {
		case <-timer.C:
			return Event{}, false
		case evt, ok := <-v.Events:
			if !ok {
				return Event{}, false // VM exited, channel closed
			}
			if evt.Type == typ {
				return evt, true
			}
			if evt.Type == EventError {
				return evt, false
			}
		}
	}
}

// RequireEvent waits for a VM event and fails the test if it doesn't arrive.
func RequireEvent(t *testing.T, v *VM, typ EventType, timeout time.Duration) Event {
	t.Helper()
	evt, ok := v.WaitForEvent(typ, timeout)
	if !ok {
		if evt.Type == EventError {
			t.Fatalf("[%s] error while waiting for %s: %s", v.Name, typ, evt.Message)
		}
		t.Fatalf("[%s] timed out waiting for %s event (%v)", v.Name, typ, timeout)
	}
	return evt
}

// WaitVMDone waits for a VM to finish with a timeout.
func WaitVMDone(t *testing.T, v *VM, timeout time.Duration) bool {
	t.Helper()
	select {
	case <-v.Done:
		return true
	case <-time.After(timeout):
		t.Errorf("%s did not finish within %v", v.Name, timeout)
		v.Kill()
		<-v.Done
		return false
	}
}

// RunfilePath resolves a Bazel runfile path using rules_go's runfiles library.
// Paths starting with an external repo name (no slash prefix) are resolved as
// external repos; paths under cluster/ are resolved under _main/.
func RunfilePath(t *testing.T, path string) string {
	t.Helper()
	// External repo paths don't get _main/ prefix.
	rloc := "_main/" + path
	if !strings.HasPrefix(path, "cluster/") {
		rloc = path
	}
	p, err := runfiles.Rlocation(rloc)
	if err != nil {
		t.Fatalf("runfile not found: %s: %v", path, err)
	}
	if _, err := os.Stat(p); err != nil {
		t.Fatalf("runfile not found: %s (resolved to %s): %v", path, p, err)
	}
	return p
}

func OutputDir(t *testing.T) string {
	t.Helper()
	dir := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if dir == "" {
		dir = t.TempDir()
	}
	return dir
}

// RunCmd runs a command, logging output and failing on error.
func RunCmd(t *testing.T, name string, args ...string) {
	t.Helper()
	cmd := exec.Command(name, args...)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("%s %v failed: %v", filepath.Base(name), args, err)
	}
}

// AssertProbes verifies that all required probes passed for a given topology.
func AssertProbes(t *testing.T, events []Event, topology string) {
	t.Helper()

	probes := map[string]*bool{}
	for _, e := range events {
		if e.Type == EventProbe && e.Success != nil {
			s := *e.Success
			probes[e.Message] = &s
		}
	}

	var requiredProbes []string
	switch topology {
	case "double_nat":
		requiredProbes = []string{
			"peer 1 ULA icmp",
			"peer 2 ULA icmp",
			"peer 1 ULA tcp",
			"peer 2 ULA tcp",
		}
	default:
		requiredProbes = []string{
			"ipv6 ULA icmp",
			"ipv4 peer eth0 icmp",
			"ipv6 ULA tcp",
			"ipv4 peer eth0 tcp",
		}
	}
	for _, name := range requiredProbes {
		if s, ok := probes[name]; !ok {
			t.Errorf("missing probe event: %s", name)
		} else if !*s {
			t.Errorf("probe failed: %s", name)
		}
	}

	for _, e := range events {
		if e.Type == EventError {
			t.Errorf("VM error: %s (%s)", e.Message, e.Error)
		}
	}
}
