package proxy

import (
	"container/list"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

const (
	// maxUpstreamPools bounds how many distinct upstreams keep idle connections; the least recently
	// used one beyond it has its idle connections closed.
	maxUpstreamPools = 256
	// Idle connections per upstream. HTTP/2 multiplexes onto one; HTTP/1.1 clients such as Bazel's
	// downloader fetch a few files at a time.
	maxIdlePerUpstream     = 4
	upstreamIdleTimeout    = 90 * time.Second
	upstreamHandshakeLimit = 10 * time.Second
)

// upstreamKey is everything an upstream connection is made for. A connection is reused only by a
// request that agrees on all of it: the authority admitted, which is also the server name its TLS
// verified; the address that request's admission pinned; and how the protocol is spoken.
type upstreamKey struct {
	// target is the admitted host:port, lowercased as the transport dials it.
	target string
	dialed string
	scheme string
	// h2c is prior-knowledge HTTP/2 over cleartext; attemptHTTP2 negotiates it over TLS.
	h2c, attemptHTTP2 bool
}

// upstreams keeps one transport per upstreamKey, so a pooled connection never carries a request to
// an address other than the one its admission pinned: a host that re-resolves to another address
// gets another key, and the old connections idle out.
type upstreams struct {
	roots *x509.CertPool

	mu    sync.Mutex
	pools map[upstreamKey]*list.Element
	// recent orders the keys most recently used first.
	recent *list.List
}

type upstreamPool struct {
	key       upstreamKey
	transport *http.Transport
}

func newUpstreams(roots *x509.CertPool) *upstreams {
	return &upstreams{roots: roots, pools: map[upstreamKey]*list.Element{}, recent: list.New()}
}

// transport returns the transport for key, creating it and evicting the least recently used one
// beyond the bound.
func (u *upstreams) transport(key upstreamKey) *http.Transport {
	u.mu.Lock()
	defer u.mu.Unlock()
	if element, found := u.pools[key]; found {
		u.recent.MoveToFront(element)
		return element.Value.(*upstreamPool).transport
	}
	transport := &http.Transport{
		Proxy: nil, DisableCompression: true,
		ForceAttemptHTTP2:   key.attemptHTTP2,
		TLSClientConfig:     &tls.Config{RootCAs: u.roots, MinVersion: tls.VersionTLS12},
		TLSHandshakeTimeout: upstreamHandshakeLimit,
		MaxIdleConns:        maxIdlePerUpstream, MaxIdleConnsPerHost: maxIdlePerUpstream,
		IdleConnTimeout: upstreamIdleTimeout,
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			// The transport lowercases the host it dials; the target was admitted as written.
			if !strings.EqualFold(address, key.target) {
				return nil, errors.New("unadmitted dial target")
			}
			var dialer net.Dialer
			return dialer.DialContext(ctx, network, key.dialed)
		},
	}
	// A normal http.Transport speaks HTTP/1 to plaintext upstreams. h2c needs prior-knowledge HTTP/2
	// selected explicitly, with HTTP/1 off.
	if key.h2c {
		transport.Protocols = new(http.Protocols)
		transport.Protocols.SetUnencryptedHTTP2(true)
	}
	u.pools[key] = u.recent.PushFront(&upstreamPool{key: key, transport: transport})
	if u.recent.Len() > maxUpstreamPools {
		oldest := u.recent.Remove(u.recent.Back()).(*upstreamPool)
		delete(u.pools, oldest.key)
		// Connections still carrying a response finish it, then idle out.
		oldest.transport.CloseIdleConnections()
	}
	return transport
}

// closeIdle closes every idle upstream connection, for a drain whose requests have finished.
func (u *upstreams) closeIdle() {
	u.mu.Lock()
	defer u.mu.Unlock()
	for element := u.recent.Front(); element != nil; element = element.Next() {
		element.Value.(*upstreamPool).transport.CloseIdleConnections()
	}
}
