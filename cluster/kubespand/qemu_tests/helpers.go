package qemu_tests

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/big"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	stateclient "github.com/cosi-project/runtime/pkg/state/protobuf/client"
	"github.com/siderolabs/talos/pkg/machinery/resources/kubespan"

	"github.com/bazelbuild/rules_go/go/runfiles"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// Runfile paths for shared test artifacts.
const (
	VmlinuzPath        = "cluster/kubespand/qemu_tests/vmlinuz-virt"
	DiscoveryInitramfs = "cluster/kubespand/qemu_tests/vms/discovery/initramfs.cpio.gz"
	RouterInitramfs    = "cluster/kubespand/qemu_tests/vms/router/initramfs.cpio.gz"
	KubespanInitramfs  = "cluster/kubespand/qemu_tests/vms/kubespan/initramfs.cpio.gz"
	DoublenatInitramfs = "cluster/kubespand/qemu_tests/vms/doublenat/initramfs.cpio.gz"
	NftInitramfs       = "cluster/kubespand/qemu_tests/nft/initramfs.cpio.gz"
	TrustdInitramfs    = "cluster/kubespand/qemu_tests/vms/trustd/initramfs.cpio.gz"
	// Talos nocloud image built by genrule — under _main/ prefix.
	TalosNocloudImagePath = "cluster/kubespand/qemu_tests/talos/nocloud-amd64.qcow2"

	// Pre-generated Talos configs (committed as testdata).
	KubespanCPConfig      = "cluster/kubespand/qemu_tests/talos/testdata/cp-kubespan.yaml"
	KubespanCPCrossConfig = "cluster/kubespand/qemu_tests/talos/testdata/cp-kubespan-cross.yaml"
	TalosVPSConfig        = "cluster/kubespand/qemu_tests/talos/testdata/vps-controlplane.yaml"
	TalosNAT1Config       = "cluster/kubespand/qemu_tests/talos/testdata/nat1-worker.yaml"
	TalosNAT2Config       = "cluster/kubespand/qemu_tests/talos/testdata/nat2-worker.yaml"
	TalosConfig           = "cluster/kubespand/qemu_tests/talos/testdata/talosconfig.yaml"
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

// RequireAllEvents waits for multiple VMs to emit the given event type in
// parallel, with a single shared deadline. Fails the test if any VM doesn't
// produce the event within the timeout.
func RequireAllEvents(t *testing.T, vms []*VM, typ EventType, timeout time.Duration) {
	t.Helper()

	type result struct {
		vm  *VM
		evt Event
		ok  bool
	}

	ch := make(chan result, len(vms))
	for _, vm := range vms {
		go func(v *VM) {
			evt, ok := v.WaitForEvent(typ, timeout)
			ch <- result{vm: v, evt: evt, ok: ok}
		}(vm)
	}

	for range vms {
		res := <-ch
		if !res.ok {
			if res.evt.Type == EventError {
				t.Fatalf("[%s] error while waiting for %s: %s", res.vm.Name, typ, res.evt.Message)
			}
			t.Fatalf("[%s] timed out waiting for %s event (%v)", res.vm.Name, typ, timeout)
		}
	}
}

// CleanupVMs registers a t.Cleanup that kills all VMs and saves their logs.
func CleanupVMs(t *testing.T, vms []*VM, outDir string) {
	t.Helper()
	t.Cleanup(func() {
		KillAndWait(vms...)
		for _, vm := range vms {
			vm.SaveLogs(t, outDir)
		}
	})
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
// ReadRunfile resolves a Bazel runfile path and reads the file contents.
func ReadRunfile(t *testing.T, path string) []byte {
	t.Helper()
	p := RunfilePath(t, path)
	data, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return data
}

func RunCmd(t *testing.T, name string, args ...string) {
	t.Helper()
	cmd := exec.Command(name, args...)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("%s %v failed: %v", filepath.Base(name), args, err)
	}
}

// Stopwatch tracks elapsed time between labeled phases for test profiling.
type Stopwatch struct {
	t      *testing.T
	start  time.Time
	last   time.Time
	phases []Phase
}

// Phase records the name and duration of a test phase.
type Phase struct {
	Name     string        `json:"name"`
	Duration time.Duration `json:"duration"`
	Elapsed  time.Duration `json:"elapsed"`
}

// NewStopwatch creates a stopwatch and logs the start time.
func NewStopwatch(t *testing.T) *Stopwatch {
	t.Helper()
	now := time.Now()
	return &Stopwatch{t: t, start: now, last: now}
}

// Lap records a named phase and logs the time since the last lap.
func (s *Stopwatch) Lap(name string) {
	s.t.Helper()
	now := time.Now()
	dur := now.Sub(s.last)
	elapsed := now.Sub(s.start)
	s.phases = append(s.phases, Phase{Name: name, Duration: dur, Elapsed: elapsed})
	s.t.Logf("[stopwatch] %s: %s (total %s)", name, dur.Round(time.Millisecond), elapsed.Round(time.Millisecond))
	s.last = now
}

// Summary logs all phases and total time, and saves a JSON artifact.
func (s *Stopwatch) Summary(outDir string) {
	s.t.Helper()
	total := time.Since(s.start)
	s.t.Logf("[stopwatch] === TIMING SUMMARY ===")
	for _, p := range s.phases {
		pct := float64(p.Duration) / float64(total) * 100
		s.t.Logf("[stopwatch]   %-40s %10s  (%4.1f%%)", p.Name, p.Duration.Round(time.Millisecond), pct)
	}
	s.t.Logf("[stopwatch]   %-40s %10s", "TOTAL", total.Round(time.Millisecond))

	if outDir != "" {
		type summaryJSON struct {
			Phases []struct {
				Name       string  `json:"name"`
				DurationMs int64   `json:"duration_ms"`
				ElapsedMs  int64   `json:"elapsed_ms"`
				Pct        float64 `json:"pct"`
			} `json:"phases"`
			TotalMs int64 `json:"total_ms"`
		}
		var sj summaryJSON
		for _, p := range s.phases {
			sj.Phases = append(sj.Phases, struct {
				Name       string  `json:"name"`
				DurationMs int64   `json:"duration_ms"`
				ElapsedMs  int64   `json:"elapsed_ms"`
				Pct        float64 `json:"pct"`
			}{
				Name:       p.Name,
				DurationMs: p.Duration.Milliseconds(),
				ElapsedMs:  p.Elapsed.Milliseconds(),
				Pct:        float64(p.Duration) / float64(total) * 100,
			})
		}
		sj.TotalMs = total.Milliseconds()
		data, _ := json.MarshalIndent(sj, "", "  ")
		SaveArtifact(s.t, outDir, "timing.json", string(data))
	}
}

// WaitForDiscoveryHTTP polls the discovery service's HTTP endpoint on the
// forwarded mgmt port until it responds (or times out).
func WaitForDiscoveryHTTP(t *testing.T, port int, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	url := fmt.Sprintf("http://127.0.0.1:%d/", port)
	client := &http.Client{Timeout: 2 * time.Second}
	for time.Now().Before(deadline) {
		resp, err := client.Get(url)
		if err == nil {
			resp.Body.Close()
			t.Logf("discovery HTTP ready on port %d", port)
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
	t.Fatalf("discovery HTTP not ready after %v on port %d", timeout, port)
}

// MgmtNIC returns QEMU args for a user-mode NIC with a port forwarded to the host.
func MgmtNIC(hostPort, guestPort int, mac string) []string {
	return []string{
		"-netdev", fmt.Sprintf("user,id=mgmt,hostfwd=tcp::%d-:%d", hostPort, guestPort),
		"-device", fmt.Sprintf("virtio-net-pci,netdev=mgmt,mac=%s", mac),
	}
}

func probeOK(probes map[string]*bool, name string) bool {
	s, ok := probes[name]
	return ok && *s
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

	switch topology {
	case "double_nat":
		// In double NAT, NAT2 can reach VPS (directly routable through
		// Router-B masquerade) but NOT NAT1 (behind Router-A's separate
		// NAT — endpoint-dependent filtering blocks cross-NAT traffic).
		// Require at least one peer's ICMP and TCP probes to pass.
		icmpOK := probeOK(probes, fmt.Sprintf(ProbePeerULAICMPFmt, 1)) || probeOK(probes, fmt.Sprintf(ProbePeerULAICMPFmt, 2))
		tcpOK := probeOK(probes, fmt.Sprintf(ProbePeerULATCPFmt, 1)) || probeOK(probes, fmt.Sprintf(ProbePeerULATCPFmt, 2))
		if !icmpOK {
			t.Errorf("no peer ULA ICMP probe succeeded (need at least VPS)")
		}
		if !tcpOK {
			t.Errorf("no peer ULA TCP probe succeeded (need at least VPS)")
		}
	default:
		for _, name := range []string{
			ProbeIPv6ULAICMP,
			ProbeIPv4Eth0ICMP,
			ProbeIPv6ULATCP,
			ProbeIPv4Eth0TCP,
		} {
			if s, ok := probes[name]; !ok {
				t.Errorf("missing probe event: %s", name)
			} else if !*s {
				t.Errorf("probe failed: %s", name)
			}
		}
	}

	for _, e := range events {
		if e.Type == EventError {
			t.Errorf("VM error: %s (%s)", e.Message, e.Error)
		}
	}
}

// PollKubespandPeerStatus connects to kubespand's TCP COSI API and polls
// PeerStatus resources until at least minPeers peers report state "up".
// Other discovered peers may be in any state (unknown, down).
func PollKubespandPeerStatus(t *testing.T, addr string, minPeers int, timeout time.Duration) ([]KubespanPeerResult, error) {
	t.Helper()

	deadline := time.Now().Add(timeout)
	var lastErr string

	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		conn, err := grpc.NewClient(addr,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if err != nil {
			cancel()
			lastErr = err.Error()
			t.Logf("kubespand COSI connect (waiting): %s", lastErr)
			time.Sleep(1 * time.Second)
			continue
		}

		st := state.WrapCore(stateclient.NewAdapter(v1alpha1.NewStateClient(conn)))
		list, err := safe.StateListAll[*kubespan.PeerStatus](ctx, st)
		cancel()
		conn.Close()

		if err != nil {
			lastErr = err.Error()
			t.Logf("kubespand COSI poll (waiting): %s", lastErr)
			time.Sleep(1 * time.Second)
			continue
		}

		var peers []KubespanPeerResult
		for it := list.Iterator(); it.Next(); {
			ps := it.Value()
			peers = append(peers, KubespanPeerResult{
				Label:    ps.TypedSpec().Label,
				State:    ps.TypedSpec().State,
				Endpoint: ps.TypedSpec().Endpoint.String(),
			})
		}

		upCount := 0
		for _, p := range peers {
			if p.State == kubespan.PeerStateUp {
				upCount++
			}
		}

		var peerSummary strings.Builder
		for i, p := range peers {
			if i > 0 {
				peerSummary.WriteString("; ")
			}
			fmt.Fprintf(&peerSummary, "%s state=%s ep=%s", p.Label, p.State, p.Endpoint)
		}
		t.Logf("kubespand COSI poll: %d peers, %d up (need %d) [%s]", len(peers), upCount, minPeers, peerSummary.String())

		if upCount >= minPeers {
			return peers, nil
		}

		time.Sleep(1 * time.Second)
	}

	return nil, fmt.Errorf("timeout after %v waiting for %d peers up, last error: %s", timeout, minPeers, lastErr)
}
