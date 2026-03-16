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
	"gopkg.in/yaml.v3"

	"github.com/agentydragon/ducktape/cluster/kubespand/agentconfig"
	pb "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/probepb"

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

// Well-known guest ports for the management NIC.
const (
	// ProbeServerGuestPort is the port the probe gRPC server listens on inside the VM.
	ProbeServerGuestPort = 50200
	// COSIGuestPort is the port kubespand's COSI API listens on inside the VM.
	COSIGuestPort = 50100
)

// mgmtMAC is the MAC address assigned to the management NIC by BootVM.
// Must match initlib.MgmtMAC so the VM init can find it.
const mgmtMAC = "52:54:00:aa:00:01"

// VM represents a running QEMU VM.
type VM struct {
	Name   string
	Done   chan struct{}
	cmd    *exec.Cmd
	rawLog strings.Builder
	mu     sync.Mutex

	t         *testing.T
	probeAddr string         // "127.0.0.1:<port>" for probe gRPC, always set by BootVM
	cosiAddr  string         // "127.0.0.1:<port>" for COSI API, empty if not forwarded
	forwards  map[int]string // guestPort -> "127.0.0.1:<hostPort>"
}

func (v *VM) Wait() {
	<-v.Done
}

func (v *VM) Kill() {
	if v.cmd != nil && v.cmd.Process != nil {
		v.cmd.Process.Kill()
	}
}

func (v *VM) GetRawLog() string {
	v.mu.Lock()
	defer v.mu.Unlock()
	return v.rawLog.String()
}

// SaveLogs saves the raw log artifact for this VM.
func (v *VM) SaveLogs(t *testing.T, dir string) {
	t.Helper()
	SaveArtifact(t, dir, v.Name+".log", v.GetRawLog())
}

// BootVM starts a QEMU VM with the given kernel cmdline args.
// A management NIC with probe server port forwarding is always added.
// meshNICs are the multicast NICs (from McastNIC calls).
// extraForwards are additional port forwards on the mgmt NIC (e.g., COSI, HTTP).
// PortForwards with HostPort==0 get a random port assigned.
func BootVM(t *testing.T, name string, vmlinuz, initramfs string, kernelArgs string, meshNICs []string, extraForwards ...PortForward) *VM {
	t.Helper()

	probePort := RandomPort()
	allForwards := []PortForward{{HostPort: probePort, GuestPort: ProbeServerGuestPort}}
	forwards := map[int]string{
		ProbeServerGuestPort: fmt.Sprintf("127.0.0.1:%d", probePort),
	}
	var cosiAddr string
	for _, ef := range extraForwards {
		if ef.HostPort == 0 {
			ef.HostPort = RandomPort()
		}
		allForwards = append(allForwards, ef)
		addr := fmt.Sprintf("127.0.0.1:%d", ef.HostPort)
		forwards[ef.GuestPort] = addr
		if ef.GuestPort == COSIGuestPort {
			cosiAddr = addr
		}
	}

	mgmtArgs := MgmtNICMulti(allForwards, mgmtMAC)

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
	args = append(args, meshNICs...)
	args = append(args, mgmtArgs...)

	v := StartVM(t, name, exec.Command("qemu-system-x86_64", args...))
	v.t = t
	v.probeAddr = fmt.Sprintf("127.0.0.1:%d", probePort)
	v.cosiAddr = cosiAddr
	v.forwards = forwards
	return v
}

// StartVM starts a QEMU process from a pre-built command and returns a VM.
// Use this for custom QEMU configurations (e.g., Talos VMs with CIDATA drives).
func StartVM(t *testing.T, name string, cmd *exec.Cmd) *VM {
	t.Helper()

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("stdout pipe: %v", err)
	}
	cmd.Stderr = cmd.Stdout

	v := &VM{
		Name: name,
		Done: make(chan struct{}),
		cmd:  cmd,
	}

	if err := cmd.Start(); err != nil {
		t.Fatalf("start QEMU %s: %v", name, err)
	}

	go func() {
		defer close(v.Done)
		scanner := bufio.NewScanner(stdout)
		scanner.Buffer(make([]byte, 0, 256*1024), 256*1024)
		for scanner.Scan() {
			line := scanner.Text()
			v.mu.Lock()
			v.rawLog.WriteString(line)
			v.rawLog.WriteByte('\n')
			v.mu.Unlock()
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

func RandomBase64(n int) string {
	buf := make([]byte, n)
	rand.Read(buf)
	return base64.StdEncoding.EncodeToString(buf)
}

func RandomPort() int {
	n, _ := rand.Int(rand.Reader, big.NewInt(50000))
	return 10000 + int(n.Int64())
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
func RunfilePath(t *testing.T, path string) string {
	t.Helper()
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

func NewStopwatch(t *testing.T) *Stopwatch {
	t.Helper()
	now := time.Now()
	return &Stopwatch{t: t, start: now, last: now}
}

func (s *Stopwatch) Lap(name string) {
	s.t.Helper()
	now := time.Now()
	dur := now.Sub(s.last)
	elapsed := now.Sub(s.start)
	s.phases = append(s.phases, Phase{Name: name, Duration: dur, Elapsed: elapsed})
	s.t.Logf("[stopwatch] %s: %s (total %s)", name, dur.Round(time.Millisecond), elapsed.Round(time.Millisecond))
	s.last = now
}

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

// ForwardAddr returns the host-side address for a forwarded guest port.
func (v *VM) ForwardAddr(guestPort int) string {
	return v.forwards[guestPort]
}

// WaitForDiscoveryHTTP polls the discovery service's HTTP endpoint on the
// forwarded mgmt port until it responds (or times out).
func WaitForDiscoveryHTTP(t *testing.T, addr string, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	url := fmt.Sprintf("http://%s/", addr)
	client := &http.Client{Timeout: 2 * time.Second}
	for time.Now().Before(deadline) {
		resp, err := client.Get(url)
		if err == nil {
			resp.Body.Close()
			t.Logf("discovery HTTP ready at %s", addr)
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
	t.Fatalf("discovery HTTP not ready after %v at %s", timeout, addr)
}

// MgmtNIC returns QEMU args for a user-mode NIC with a port forwarded to the host.
func MgmtNIC(hostPort, guestPort int, mac string) []string {
	return []string{
		"-netdev", fmt.Sprintf("user,id=mgmt,hostfwd=tcp::%d-:%d", hostPort, guestPort),
		"-device", fmt.Sprintf("virtio-net-pci,netdev=mgmt,mac=%s", mac),
	}
}

// PortForward maps a host port to a guest port for QEMU user-mode networking.
type PortForward struct {
	HostPort  int
	GuestPort int
}

// MgmtNICMulti returns QEMU args for a user-mode NIC with multiple port forwards.
func MgmtNICMulti(forwards []PortForward, mac string) []string {
	var fwds []string
	for _, f := range forwards {
		fwds = append(fwds, fmt.Sprintf("hostfwd=tcp::%d-:%d", f.HostPort, f.GuestPort))
	}
	return []string{
		"-netdev", fmt.Sprintf("user,id=mgmt,%s", strings.Join(fwds, ",")),
		"-device", fmt.Sprintf("virtio-net-pci,netdev=mgmt,mac=%s", mac),
	}
}

// NewTestAgentConfig returns an AgentConfig with common test defaults.
// Callers can override fields after construction.
func NewTestAgentConfig(clusterID, sharedSecret, discoveryAddr string) agentconfig.AgentConfig {
	return agentconfig.AgentConfig{
		Cluster:   agentconfig.ClusterConfig{ID: clusterID, Secret: sharedSecret},
		Discovery: agentconfig.DiscoveryConfig{Endpoint: discoveryAddr, Insecure: true, MachineType: "worker"},
		Kubespan: agentconfig.KubespanConfig{
			ForceRouting:          true,
			MTU:                   1420,
			IdentityFile:          "/var/lib/kubespan/identity.yaml",
			HarvestExtraEndpoints: true,
		},
	}
}

// CreateKubespandCIDATA creates a FAT32 disk image containing the kubespand
// agent config YAML. The VM init mounts this drive and copies agent.yaml to
// /etc/kubespan/agent.yaml for kubespand to read.
func CreateKubespandCIDATA(t *testing.T, tmpDir, name string, cfg agentconfig.AgentConfig) string {
	t.Helper()

	configData, err := yaml.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal agent config: %v", err)
	}

	ciDir := filepath.Join(tmpDir, "kubespand-cidata-"+name)
	os.MkdirAll(ciDir, 0o755)
	os.WriteFile(filepath.Join(ciDir, "agent.yaml"), configData, 0o644)

	imgPath := filepath.Join(tmpDir, fmt.Sprintf("kubespand-cidata-%s.img", name))
	RunCmd(t, "dd", "if=/dev/zero", "of="+imgPath, "bs=1M", "count=1")
	RunCmd(t, "/usr/sbin/mkfs.vfat", "-n", "KUBESPAND", imgPath)
	RunCmd(t, "/usr/bin/mcopy", "-i", imgPath, filepath.Join(ciDir, "agent.yaml"), "::")

	t.Logf("created kubespand CIDATA for %s: %s (%d bytes config)", name, imgPath, len(configData))
	return imgPath
}

// CIDATADrive returns QEMU args to attach a CIDATA FAT32 image as a virtio drive.
func CIDATADrive(path string) []string {
	return []string{
		"-drive", fmt.Sprintf("file=%s,if=virtio,format=raw,readonly=on", path),
	}
}

// WaitForProbeServer polls the VM's probe gRPC server until it responds.
// A responding probe server means the VM has completed initialization.
func (v *VM) WaitForProbeServer(timeout time.Duration) bool {
	v.t.Helper()
	return v.probeWithRetry("wait for probe server", timeout,
		func(client pb.ProbeServiceClient, ctx context.Context) (bool, string) {
			_, err := client.Diagnostics(ctx, &pb.DiagnosticsRequest{})
			if err != nil {
				return false, err.Error()
			}
			return true, ""
		})
}

// WaitForProbeServers waits for multiple VMs' probe servers to respond in parallel.
func WaitForProbeServers(t *testing.T, vms []*VM, timeout time.Duration) {
	t.Helper()

	type result struct {
		vm *VM
		ok bool
	}

	ch := make(chan result, len(vms))
	for _, vm := range vms {
		go func(v *VM) {
			ch <- result{vm: v, ok: v.WaitForProbeServer(timeout)}
		}(vm)
	}

	for range vms {
		res := <-ch
		if !res.ok {
			t.Fatalf("[%s] probe server not ready after %v", res.vm.Name, timeout)
		}
	}
}

// probeWithRetry runs a probe RPC with retry until success or timeout.
func (v *VM) probeWithRetry(label string, timeout time.Duration, probeFn func(pb.ProbeServiceClient, context.Context) (bool, string)) bool {
	v.t.Helper()
	prefix := fmt.Sprintf("[%s] %s", v.Name, label)
	deadline := time.Now().Add(timeout)

	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		conn, err := grpc.NewClient(v.probeAddr,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if err != nil {
			cancel()
			v.t.Logf("%s: connect: %v", prefix, err)
			time.Sleep(1 * time.Second)
			continue
		}

		client := pb.NewProbeServiceClient(conn)
		ok, detail := probeFn(client, ctx)
		cancel()
		conn.Close()

		if ok {
			v.t.Logf("%s: success", prefix)
			return true
		}
		v.t.Logf("%s: %s (retrying)", prefix, detail)
		time.Sleep(1 * time.Second)
	}

	v.t.Errorf("%s: timed out after %v", prefix, timeout)
	return false
}

// ProbeICMP sends an ICMP probe request via the VM's probe server, retrying until success or timeout.
func (v *VM) ProbeICMP(target string, timeout time.Duration) bool {
	v.t.Helper()
	return v.probeWithRetry(fmt.Sprintf("probe ICMP→%s", target), timeout,
		func(client pb.ProbeServiceClient, ctx context.Context) (bool, string) {
			resp, err := client.ICMPProbe(ctx, &pb.ICMPProbeRequest{
				Target:         target,
				TimeoutSeconds: 10,
			})
			if err != nil {
				return false, err.Error()
			}
			if resp.Success {
				return true, ""
			}
			return false, resp.Error
		})
}

// ProbeTCP sends a TCP probe request via the VM's probe server, retrying until success or timeout.
func (v *VM) ProbeTCP(target string, port int, timeout time.Duration) bool {
	v.t.Helper()
	return v.probeWithRetry(fmt.Sprintf("probe TCP→%s:%d", target, port), timeout,
		func(client pb.ProbeServiceClient, ctx context.Context) (bool, string) {
			resp, err := client.TCPProbe(ctx, &pb.TCPProbeRequest{
				Target:         target,
				Port:           int32(port),
				TimeoutSeconds: 10,
			})
			if err != nil {
				return false, err.Error()
			}
			if resp.Success {
				return true, ""
			}
			return false, resp.Error
		})
}

// DumpDiagnostics fetches routing/WG/nftables state from the VM's probe server.
func (v *VM) DumpDiagnostics() string {
	v.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	conn, err := grpc.NewClient(v.probeAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		v.t.Logf("[%s] diagnostics connect: %v", v.Name, err)
		return ""
	}
	defer conn.Close()

	client := pb.NewProbeServiceClient(conn)
	resp, err := client.Diagnostics(ctx, &pb.DiagnosticsRequest{})
	if err != nil {
		v.t.Logf("[%s] diagnostics rpc: %v", v.Name, err)
		return ""
	}
	return resp.Output
}

// PollPeerStatus connects to kubespand's COSI API and polls PeerStatus
// resources until at least minPeers report state "up".
func (v *VM) PollPeerStatus(minPeers int, timeout time.Duration) ([]KubespanPeerResult, error) {
	v.t.Helper()

	if v.cosiAddr == "" {
		v.t.Fatalf("[%s] PollPeerStatus called but COSI not configured", v.Name)
	}

	deadline := time.Now().Add(timeout)
	var lastErr string

	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		conn, err := grpc.NewClient(v.cosiAddr,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
		)
		if err != nil {
			cancel()
			lastErr = err.Error()
			v.t.Logf("[%s] COSI connect (waiting): %s", v.Name, lastErr)
			time.Sleep(1 * time.Second)
			continue
		}

		st := state.WrapCore(stateclient.NewAdapter(v1alpha1.NewStateClient(conn)))
		list, err := safe.StateListAll[*kubespan.PeerStatus](ctx, st)
		cancel()
		conn.Close()

		if err != nil {
			lastErr = err.Error()
			v.t.Logf("[%s] COSI poll (waiting): %s", v.Name, lastErr)
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
		v.t.Logf("[%s] COSI poll: %d peers, %d up (need %d) [%s]", v.Name, len(peers), upCount, minPeers, peerSummary.String())

		if upCount >= minPeers {
			return peers, nil
		}

		time.Sleep(1 * time.Second)
	}

	return nil, fmt.Errorf("timeout after %v waiting for %d peers up, last error: %s", timeout, minPeers, lastErr)
}
