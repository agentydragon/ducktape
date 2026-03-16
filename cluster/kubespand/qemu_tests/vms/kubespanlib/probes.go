package kubespanlib

import (
	"fmt"
	"net"
	"os"
)

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
