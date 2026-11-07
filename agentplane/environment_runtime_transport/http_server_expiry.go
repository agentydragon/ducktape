package environment_runtime_transport

import (
	"crypto/tls"
	"net"
	"net/http"
	"sync"
	"time"
)

// HTTPServerTLSExpiryConnState closes HTTP connections when their authenticated
// peer certificate expires.
// StateActive is handled once, and the timer is stopped for closed or hijacked
// connections. The callback observes and closes the original *tls.Conn rather
// than wrapping it, as net/http requires that concrete connection type.
func HTTPServerTLSExpiryConnState() func(net.Conn, http.ConnState) {
	var mu sync.Mutex
	timers := map[net.Conn]func(){}
	return func(connection net.Conn, state http.ConnState) {
		mu.Lock()
		defer mu.Unlock()
		switch state {
		case http.StateActive:
			secured, ok := connection.(*tls.Conn)
			if !ok || timers[connection] != nil {
				return
			}
			timers[connection] = CloseTLSConnAtCertificateExpiry(secured, time.Time{})
		case http.StateClosed, http.StateHijacked:
			if cancel, ok := timers[connection]; ok {
				cancel()
				delete(timers, connection)
			}
		}
	}
}
