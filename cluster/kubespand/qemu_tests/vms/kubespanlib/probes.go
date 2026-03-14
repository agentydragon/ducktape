package kubespanlib

import (
	"fmt"
	"net"
	"os"
	"time"

	"golang.org/x/net/icmp"
	"golang.org/x/net/ipv4"
	"golang.org/x/net/ipv6"

	qemu_tests "github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests"
	"github.com/agentydragon/ducktape/cluster/kubespand/qemu_tests/vms/initlib"
)

// TCPProbe attempts a TCP connection to target:port with retry until timeout.
func TCPProbe(target string, port int, timeout time.Duration) bool {
	addr := net.JoinHostPort(target, fmt.Sprintf("%d", port))
	deadline := time.Now().Add(timeout)

	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", addr, 3*time.Second)
		if err == nil {
			conn.Close()
			fmt.Printf("tcp connect %s succeeded\n", addr)
			return true
		}
		time.Sleep(time.Second)
	}

	fmt.Fprintf(os.Stderr, "timeout after %s: no TCP connection to %s\n", timeout, addr)
	return false
}

// ServeTCP starts TCP listeners on the given port on both IPv4 and IPv6.
func ServeTCP(port int) (cancel func()) {
	addr := fmt.Sprintf(":%d", port)
	var listeners []net.Listener
	for _, network := range []string{"tcp4", "tcp6"} {
		ln, err := net.Listen(network, addr)
		if err != nil {
			fmt.Fprintf(os.Stderr, "serveTCP %s: %v\n", network, err)
			continue
		}
		listeners = append(listeners, ln)
		go func(l net.Listener) {
			for {
				conn, err := l.Accept()
				if err != nil {
					return
				}
				conn.Close()
			}
		}(ln)
	}
	return func() {
		for _, ln := range listeners {
			ln.Close()
		}
	}
}

// Probe sends ICMP echo requests to the target with retry until timeout.
func Probe(target string, timeout time.Duration) bool {
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
			fmt.Printf("ping %s succeeded (seq %d)\n", target, seq)
			return true
		}
		time.Sleep(time.Second)
	}

	fmt.Fprintf(os.Stderr, "timeout after %s: no ICMP echo reply from %s\n", timeout, target)
	return false
}

func ping4(target string, seq int) bool {
	conn, err := icmp.ListenPacket("ip4:icmp", "0.0.0.0")
	if err != nil {
		fmt.Fprintf(os.Stderr, "listen: %v\n", err)
		return false
	}
	defer conn.Close()

	if err := conn.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		return false
	}

	msg := icmp.Message{
		Type: ipv4.ICMPTypeEcho, Code: 0,
		Body: &icmp.Echo{ID: os.Getpid() & 0xffff, Seq: seq, Data: []byte("kubespan-probe")},
	}
	wb, err := msg.Marshal(nil)
	if err != nil {
		return false
	}

	dst, err := net.ResolveIPAddr("ip4", target)
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
	rm, err := icmp.ParseMessage(1, rb[:n])
	if err != nil {
		return false
	}
	return rm.Type == ipv4.ICMPTypeEchoReply
}

func ping6(target string, seq int) bool {
	conn, err := icmp.ListenPacket("ip6:ipv6-icmp", "::")
	if err != nil {
		fmt.Fprintf(os.Stderr, "listen: %v\n", err)
		return false
	}
	defer conn.Close()

	if err := conn.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
		return false
	}

	msg := icmp.Message{
		Type: ipv6.ICMPTypeEchoRequest, Code: 0,
		Body: &icmp.Echo{ID: os.Getpid() & 0xffff, Seq: seq, Data: []byte("kubespan-probe")},
	}
	wb, err := msg.Marshal(nil)
	if err != nil {
		return false
	}

	dst, err := net.ResolveIPAddr("ip6", target)
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
	rm, err := icmp.ParseMessage(58, rb[:n])
	if err != nil {
		return false
	}
	return rm.Type == ipv6.ICMPTypeEchoReply
}

// EmitProbe emits a probe event and dumps kubespand log on failure.
func EmitProbe(msg, target string, ok bool) {
	evt := qemu_tests.Event{Type: qemu_tests.EventProbe, Message: msg, Target: target, Success: &ok}
	if !ok {
		evt.Error = "probe failed"
		initlib.DumpLog("/tmp/kubespand.log")
	}
	initlib.EmitEvent(evt)
}

// RunProbes runs the standard 2-node probe suite (IPv6 ULA + IPv4 bridge, ICMP + TCP).
func RunProbes(peerAddr, peerBridgeIP string, tcpPort int) {
	EmitProbe("ipv6 ULA icmp", peerAddr, Probe(peerAddr, 60*time.Second))
	EmitProbe("ipv4 peer eth0 icmp", peerBridgeIP, Probe(peerBridgeIP, 60*time.Second))
	EmitProbe("ipv6 ULA tcp", fmt.Sprintf("[%s]:%d", peerAddr, tcpPort),
		TCPProbe(peerAddr, tcpPort, 30*time.Second))
	EmitProbe("ipv4 peer eth0 tcp", fmt.Sprintf("%s:%d", peerBridgeIP, tcpPort),
		TCPProbe(peerBridgeIP, tcpPort, 30*time.Second))
}

// RunDoubleNATProbes probes each peer's ULA via ICMP and TCP.
func RunDoubleNATProbes(peerAddrs []string, tcpPort int) {
	for i, addr := range peerAddrs {
		label := fmt.Sprintf("peer %d", i+1)
		EmitProbe(label+" ULA icmp", addr, Probe(addr, 60*time.Second))
		EmitProbe(label+" ULA tcp", fmt.Sprintf("[%s]:%d", addr, tcpPort),
			TCPProbe(addr, tcpPort, 30*time.Second))
	}
}
