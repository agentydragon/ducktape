package initlib

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"time"

	"golang.org/x/net/icmp"
	"golang.org/x/net/ipv4"
	"golang.org/x/net/ipv6"
	"google.golang.org/grpc"

	pb "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/probepb"
)

// ProbeServerPort is the well-known port the probe gRPC server listens on
// inside the VM. Tests forward this to a random host port via the mgmt NIC.
const ProbeServerPort = 50200

type probeServer struct {
	pb.UnimplementedProbeServiceServer
}

func (s *probeServer) ICMPProbe(_ context.Context, req *pb.ICMPProbeRequest) (*pb.ProbeResponse, error) {
	timeout := time.Duration(req.TimeoutSeconds) * time.Second
	if timeout == 0 {
		timeout = 60 * time.Second
	}
	ok := icmpProbe(req.Target, timeout)
	resp := &pb.ProbeResponse{Success: ok}
	if !ok {
		resp.Error = fmt.Sprintf("ICMP probe to %s failed after %s", req.Target, timeout)
	}
	return resp, nil
}

func (s *probeServer) TCPProbe(_ context.Context, req *pb.TCPProbeRequest) (*pb.ProbeResponse, error) {
	timeout := time.Duration(req.TimeoutSeconds) * time.Second
	if timeout == 0 {
		timeout = 30 * time.Second
	}
	ok := tcpProbe(req.Target, int(req.Port), timeout)
	resp := &pb.ProbeResponse{Success: ok}
	if !ok {
		resp.Error = fmt.Sprintf("TCP probe to %s:%d failed after %s", req.Target, req.Port, timeout)
	}
	return resp, nil
}

func (s *probeServer) Diagnostics(_ context.Context, _ *pb.DiagnosticsRequest) (*pb.DiagnosticsResponse, error) {
	return &pb.DiagnosticsResponse{Output: collectDiagnostics()}, nil
}

// StartProbeServer starts the gRPC probe server on the given address in a goroutine.
func StartProbeServer(addr string) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "probe server listen %s: %v\n", addr, err)
		return
	}
	srv := grpc.NewServer()
	pb.RegisterProbeServiceServer(srv, &probeServer{})
	go func() {
		if err := srv.Serve(ln); err != nil {
			fmt.Fprintf(os.Stderr, "probe server: %v\n", err)
		}
	}()
}

// icmpProbe sends ICMP echo requests to the target with retry until timeout.
func icmpProbe(target string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	seq := 0

	ip := net.ParseIP(target)
	isV4 := ip != nil && ip.To4() != nil

	for time.Now().Before(deadline) {
		seq++
		var ok bool
		if isV4 {
			ok = ping4(target, seq)
		} else {
			ok = ping6(target, seq)
		}
		if ok {
			return true
		}
		time.Sleep(200 * time.Millisecond)
	}
	return false
}

// tcpProbe attempts a TCP connection to target:port with retry until timeout.
func tcpProbe(target string, port int, timeout time.Duration) bool {
	addr := net.JoinHostPort(target, fmt.Sprintf("%d", port))
	deadline := time.Now().Add(timeout)

	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", addr, 3*time.Second)
		if err == nil {
			conn.Close()
			return true
		}
		time.Sleep(200 * time.Millisecond)
	}
	return false
}

// ping sends a single ICMP echo and checks for a reply. Protocol-agnostic:
// pass the appropriate network/listen/resolve/echoType/replyType/protoNum for v4 or v6.
func ping(target string, seq int, network, listenAddr, resolveNet string, echoType icmp.Type, replyType icmp.Type, protoNum int) bool {
	conn, err := icmp.ListenPacket(network, listenAddr)
	if err != nil {
		return false
	}
	defer conn.Close()

	if err := conn.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		return false
	}

	msg := icmp.Message{
		Type: echoType, Code: 0,
		Body: &icmp.Echo{ID: os.Getpid() & 0xffff, Seq: seq, Data: []byte("kubespan-probe")},
	}
	wb, err := msg.Marshal(nil)
	if err != nil {
		return false
	}

	dst, err := net.ResolveIPAddr(resolveNet, target)
	if err != nil {
		return false
	}
	if _, err := conn.WriteTo(wb, dst); err != nil {
		return false
	}

	rb := make([]byte, 1500)
	n, _, err := conn.ReadFrom(rb)
	if err != nil {
		return false
	}
	rm, err := icmp.ParseMessage(protoNum, rb[:n])
	if err != nil {
		return false
	}
	return rm.Type == replyType
}

func ping4(target string, seq int) bool {
	return ping(target, seq, "ip4:icmp", "0.0.0.0", "ip4", ipv4.ICMPTypeEcho, ipv4.ICMPTypeEchoReply, 1)
}

func ping6(target string, seq int) bool {
	return ping(target, seq, "ip6:ipv6-icmp", "::", "ip6", ipv6.ICMPTypeEchoRequest, ipv6.ICMPTypeEchoReply, 58)
}

// collectDiagnostics runs diagnostic commands and returns their combined output.
func collectDiagnostics() string {
	var buf bytes.Buffer
	run := func(name string, args ...string) {
		fmt.Fprintf(&buf, "$ %s %s\n", name, args)
		cmd := exec.Command(name, args...)
		cmd.Stdout = &buf
		cmd.Stderr = &buf
		cmd.Run()
		buf.WriteByte('\n')
	}

	run("ip", "addr", "show")
	run("ip", "rule", "show")
	run("ip", "route", "show", "table", "main")
	run("ip", "route", "show", "table", "180")
	run("wg", "show", "kubespan")
	run("nft", "list", "ruleset")
	run("cat", "/proc/sys/net/ipv4/conf/all/rp_filter")
	run("cat", "/proc/sys/net/ipv4/conf/default/rp_filter")
	run("cat", "/proc/sys/net/ipv4/conf/eth0/rp_filter")
	run("cat", "/proc/sys/net/ipv4/ip_forward")

	return buf.String()
}
