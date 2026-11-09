package environment_runtime_transport

import (
	"crypto/tls"
	"errors"
	"net"
	"net/http"
	"sync"
	"time"
)

// ConfigureHTTPServerTLSExpiry wraps config.GetCertificate and returns a ConnState
// hook that closes connections when either TLS leaf expires. The config must use
// GetCertificate as its only certificate selector. It disables server-side TLS
// session resumption because resumed handshakes skip GetCertificate and therefore
// do not expose the local leaf whose expiry must bound the connection. The hook
// observes and closes the original *tls.Conn so net/http can retain HTTP/2 support.
func ConfigureHTTPServerTLSExpiry(config *tls.Config) (func(net.Conn, http.ConnState), error) {
	if config == nil || config.GetCertificate == nil || config.GetConfigForClient != nil || len(config.Certificates) != 0 {
		return nil, errors.New("HTTP server TLS expiry requires GetCertificate as the only certificate selector")
	}
	config.SessionTicketsDisabled = true
	var mu sync.Mutex
	selectedExpiry := map[net.Conn]time.Time{}
	timers := map[*tls.Conn]func(){}
	getCertificate := config.GetCertificate
	config.GetCertificate = func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
		certificate, err := getCertificate(hello)
		if err != nil {
			return nil, err
		}
		if hello.Conn == nil {
			return nil, errors.New("TLS handshake has no underlying connection")
		}
		expiresAt, err := certificateNotAfter(certificate)
		if err != nil {
			return nil, err
		}
		mu.Lock()
		selectedExpiry[hello.Conn] = expiresAt
		mu.Unlock()
		return certificate, nil
	}
	return func(connection net.Conn, state http.ConnState) {
		if state != http.StateActive && state != http.StateClosed && state != http.StateHijacked {
			return
		}
		secured, ok := connection.(*tls.Conn)
		if !ok {
			return
		}
		switch state {
		case http.StateActive:
			mu.Lock()
			if timers[secured] != nil {
				mu.Unlock()
				return
			}
			expiresAt, ok := selectedExpiry[secured.NetConn()]
			delete(selectedExpiry, secured.NetConn())
			if ok {
				timers[secured] = CloseTLSConnAtCertificateExpiry(secured, expiresAt)
			}
			mu.Unlock()
			if !ok {
				_ = secured.Close()
			}
		case http.StateClosed, http.StateHijacked:
			mu.Lock()
			delete(selectedExpiry, secured.NetConn())
			cancel := timers[secured]
			delete(timers, secured)
			mu.Unlock()
			if cancel != nil {
				cancel()
			}
		}
	}, nil
}
