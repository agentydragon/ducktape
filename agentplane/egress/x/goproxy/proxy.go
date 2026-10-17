// Package goproxyspike evaluates a central egress transport built on the Go standard
// library alone: net/http for CONNECT and MITM serving, httputil.ReverseProxy for
// forwarding. It is not a replacement service: the caller supplies bounded-fresh shared
// policy snapshots and whole-value header credentials; unsupported policy shapes fail
// closed.
package goproxyspike

import (
	"bufio"
	"context"
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httputil"
	"net/netip"
	"slices"
	"sync"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/auth"
	"google.golang.org/grpc/metadata"
)

// HeaderCredential intentionally supports only wholeValue targets in this spike.
// It is fixture configuration, not a second EgressCredential CR definition.
type HeaderCredential struct {
	Header string
	Value  string
}

type Snapshot struct {
	Sets        []policyapi.PolicySet
	Bindings    []policyapi.PolicyBinding
	Credentials map[string]HeaderCredential
	FreshUntil  time.Time
}

// Options requires explicit trust roots, interception identity, policy and auth.
// Resolve is a dependency seam: production callers would use net.DefaultResolver.LookupNetIP.
type Options struct {
	CA           tls.Certificate
	Roots        *x509.CertPool
	Auth         *auth.Resolver
	ReadSnapshot func() (Snapshot, error)
	Resolve      func(context.Context, string, string) ([]netip.Addr, error)
	Exempt       []netip.Prefix
}

type tunnel struct {
	mu        sync.Mutex
	bearer    string
	principal auth.Principal
}

// tlsRecordHandshake is the first byte of a TLS ClientHello.
const tlsRecordHandshake = 0x16

type server struct {
	options Options
	leaves  *leafIssuer
}

// New returns a proxy for CONNECT tunnels, intercepted with options.CA, and for
// absolute-form plain HTTP. Every request, inside a tunnel or not, passes the same
// authentication, policy, credential and address gates before anything is dialed.
func New(options Options) (http.Handler, error) {
	if len(options.CA.Certificate) == 0 || options.CA.PrivateKey == nil || options.Roots == nil || options.Auth == nil || options.ReadSnapshot == nil || options.Resolve == nil {
		return nil, errors.New("CA, upstream roots, authentication, snapshot source and resolver are required")
	}
	leaves, err := newLeafIssuer(options.CA)
	if err != nil {
		return nil, err
	}
	return &server{options: options, leaves: leaves}, nil
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodConnect:
		s.connect(w, r)
	case r.URL.IsAbs():
		s.forward(w, r, &tunnel{})
	default:
		http.Error(w, "proxy requests only", http.StatusBadRequest)
	}
}

func (s *server) connect(w http.ResponseWriter, r *http.Request) {
	state := &tunnel{}
	principal, err := identify(r, state, s.options.Auth)
	if err != nil {
		deny(w, "token-rejected")
		return
	}
	if _, _, err := admit(r, principal, s.options.ReadSnapshot, true); err != nil {
		deny(w, "no-rule")
		return
	}
	conn, buffered, err := http.NewResponseController(w).Hijack()
	if err != nil {
		http.Error(w, "CONNECT needs HTTP/1.1", http.StatusHTTPVersionNotSupported)
		return
	}
	defer conn.Close()
	// Acknowledge without contacting the upstream. Inner admission installs the only
	// usable transport, so a denied path cannot leak a dial.
	if _, err := io.WriteString(conn, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		return
	}
	s.serveTunnel(conn, buffered.Reader, r.Host, state)
}

// serveTunnel serves one CONNECT tunnel: TLS (HTTP/1.1 or h2 by ALPN) intercepted
// with a forged leaf, or cleartext HTTP/1.1 or prior-knowledge h2c.
func (s *server) serveTunnel(conn net.Conn, buffered *bufio.Reader, authority string, state *tunnel) {
	if err := conn.SetReadDeadline(time.Now().Add(30 * time.Second)); err != nil {
		return
	}
	first, err := buffered.Peek(1)
	if err != nil || conn.SetReadDeadline(time.Time{}) != nil {
		return
	}
	scheme := "http"
	// The close notification sits below TLS so the server is handed the *tls.Conn itself
	// and negotiates h2 from its ALPN result.
	closed := make(chan struct{})
	var client net.Conn = &closeNotifyConn{Conn: &readBufferedConn{Conn: conn, reader: buffered}, closed: closed}
	if first[0] == tlsRecordHandshake {
		scheme = "https"
		fallbackName, _, _ := net.SplitHostPort(authority)
		secured := tls.Server(client, &tls.Config{
			MinVersion: tls.VersionTLS12, NextProtos: []string{"h2", "http/1.1"},
			GetCertificate: func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
				if hello.ServerName != "" {
					return s.leaves.issue(hello.ServerName)
				}
				return s.leaves.issue(fallbackName)
			},
		})
		handshake, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := secured.HandshakeContext(handshake); err != nil {
			log.Printf("tunnel TLS handshake: %v", err)
			return
		}
		client = secured
	}
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetHTTP2(true)
	protocols.SetUnencryptedHTTP2(true)
	inner := &http.Server{
		Protocols: protocols, ErrorLog: log.New(io.Discard, "", 0), ReadHeaderTimeout: 30 * time.Second,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			// The inner authority is authoritative: it is what policy and the pinned dial see.
			host := req.Host
			if host == "" {
				host = authority
			}
			req.URL.Scheme, req.URL.Host = scheme, host
			s.forward(w, req, state)
		}),
	}
	// http.Server serves one accepted connection; the listener then blocks until it ends.
	_ = inner.Serve(&singleConnListener{conn: client, closed: closed})
}

// forward gates one proxied request (target in req.URL) and relays it only if every gate passes.
func (s *server) forward(w http.ResponseWriter, req *http.Request, state *tunnel) {
	options := s.options
	principal, err := identify(req, state, options.Auth)
	// Always consume both hop headers, including on denial.
	req.Header.Del("Proxy-Authorization")
	projected := req.Header.Values("X-Agentplane-Workload-Token")
	req.Header.Del("X-Agentplane-Workload-Token")
	if err != nil {
		deny(w, "token-rejected")
		return
	}
	// Projected token substitution is deliberately outside this spike's subset.
	if len(projected) != 0 {
		deny(w, "credential-unavailable")
		return
	}
	rule, snapshot, err := admit(req, principal, options.ReadSnapshot, false)
	if err != nil {
		deny(w, "no-rule")
		return
	}
	if err := substitute(req, rule, snapshot.Credentials); err != nil {
		deny(w, "placeholder-unresolved")
		return
	}
	address, err := pin(req.Context(), req.URL.Hostname(), rule.ClusterInternal, options)
	if err != nil {
		deny(w, "address-forbidden")
		return
	}
	// Recheck after asynchronous DNS and before constructing a dial-capable transport.
	current, latest, err := admit(req, principal, options.ReadSnapshot, false)
	if err != nil || !sameRule(rule, current) || !sameCredentials(snapshot.Credentials, latest.Credentials) {
		deny(w, "unavailable")
		return
	}
	port := req.URL.Port()
	if port == "" {
		port = "80"
		if req.URL.Scheme == "https" {
			port = "443"
		}
	}
	authority := req.URL.Host
	transport := &http.Transport{
		Proxy: nil, DisableKeepAlives: true, DisableCompression: true,
		ForceAttemptHTTP2: req.Header.Get("Upgrade") == "", TLSClientConfig: &tls.Config{RootCAs: options.Roots, MinVersion: tls.VersionTLS12},
		DialContext: func(ctx context.Context, network, target string) (net.Conn, error) {
			if target != net.JoinHostPort(req.URL.Hostname(), port) {
				return nil, errors.New("unadmitted dial target")
			}
			var d net.Dialer
			return d.DialContext(ctx, network, net.JoinHostPort(address.String(), port))
		},
	}
	// A normal http.Transport speaks HTTP/1 to plaintext upstreams. h2c needs
	// prior-knowledge HTTP/2 selected explicitly, with HTTP/1 off.
	if req.URL.Scheme == "http" && req.ProtoMajor == 2 {
		transport.Protocols = new(http.Protocols)
		transport.Protocols.SetUnencryptedHTTP2(true)
	}
	(&httputil.ReverseProxy{
		Rewrite: func(out *httputil.ProxyRequest) { out.Out.Host = "" },
		Transport: roundTripperFunc(func(out *http.Request) (*http.Response, error) {
			if out.URL.Host != authority {
				return nil, errors.New("authority changed after admission")
			}
			return transport.RoundTrip(out)
		}),
		// Upstream errors can contain private URLs: report only the status.
		ErrorLog:     log.New(io.Discard, "", 0),
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) { w.WriteHeader(http.StatusBadGateway) },
	}).ServeHTTP(w, req)
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

// readBufferedConn serves bytes the CONNECT parser already read ahead before the live connection.
type readBufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *readBufferedConn) Read(p []byte) (int, error) { return c.reader.Read(p) }

// closeNotifyConn reports Close so the single-connection listener can end its server.
type closeNotifyConn struct {
	net.Conn
	closed chan struct{}
	once   sync.Once
}

func (c *closeNotifyConn) Close() error {
	c.once.Do(func() { close(c.closed) })
	return c.Conn.Close()
}

type singleConnListener struct {
	conn   net.Conn
	closed <-chan struct{}
	once   sync.Once
}

func (l *singleConnListener) Accept() (net.Conn, error) {
	var conn net.Conn
	l.once.Do(func() { conn = l.conn })
	if conn != nil {
		return conn, nil
	}
	<-l.closed
	return nil, net.ErrClosed
}

func (l *singleConnListener) Close() error   { return nil }
func (l *singleConnListener) Addr() net.Addr { return l.conn.LocalAddr() }

// leafIssuer signs a short-lived interception certificate per handshake. One leaf key
// is reused and nothing is cached, so client-chosen names cannot grow memory.
type leafIssuer struct {
	ca     *x509.Certificate
	signer crypto.Signer
	key    *ecdsa.PrivateKey
}

func newLeafIssuer(ca tls.Certificate) (*leafIssuer, error) {
	parsed, err := x509.ParseCertificate(ca.Certificate[0])
	if err != nil {
		return nil, err
	}
	signer, ok := ca.PrivateKey.(crypto.Signer)
	if !ok {
		return nil, errors.New("interception CA key cannot sign")
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	return &leafIssuer{ca: parsed, signer: signer, key: key}, nil
}

func (i *leafIssuer) issue(name string) (*tls.Certificate, error) {
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 127))
	if err != nil {
		return nil, err
	}
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber: serial, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, BasicConstraintsValid: true,
	}
	if i.ca.NotAfter.Before(template.NotAfter) {
		template.NotAfter = i.ca.NotAfter
	}
	if address, err := netip.ParseAddr(name); err == nil {
		template.IPAddresses = []net.IP{address.AsSlice()}
	} else {
		template.DNSNames = []string{name}
	}
	der, err := x509.CreateCertificate(rand.Reader, template, i.ca, i.key.Public(), i.signer)
	if err != nil {
		return nil, err
	}
	return &tls.Certificate{Certificate: [][]byte{der}, PrivateKey: i.key}, nil
}

func identify(req *http.Request, state *tunnel, resolver *auth.Resolver) (auth.Principal, error) {
	state.mu.Lock()
	defer state.mu.Unlock()
	values, supplied := req.Header["Proxy-Authorization"]
	bearer := state.bearer
	if supplied {
		// A replacement always clears earlier context, including malformed values.
		state.bearer = ""
		state.principal = auth.Principal{}
		if len(values) != 1 {
			return auth.Principal{}, errors.New("ambiguous hop bearer")
		}
		bearer = values[0]
	}
	principal, err := resolver.Resolve(metadata.NewIncomingContext(req.Context(), metadata.Pairs("authorization", bearer)))
	if err != nil {
		state.bearer = ""
		state.principal = auth.Principal{}
		return auth.Principal{}, err
	}
	if !supplied && state.principal != (auth.Principal{}) && state.principal != principal {
		state.bearer = ""
		state.principal = auth.Principal{}
		return auth.Principal{}, errors.New("authenticated context changed")
	}
	state.bearer, state.principal = bearer, principal
	return principal, nil
}

func admit(req *http.Request, principal auth.Principal, read func() (Snapshot, error), connect bool) (policyapi.Rule, Snapshot, error) {
	snapshot, err := read()
	if err != nil || !time.Now().Before(snapshot.FreshUntil) {
		return policyapi.Rule{}, snapshot, errors.New("snapshot unavailable")
	}
	for _, binding := range snapshot.Bindings {
		if binding.Spec.Subject.Namespace != principal.Account.Namespace || binding.Spec.Subject.Name != principal.Account.Name ||
			(binding.Spec.ExpiresAt != nil && !time.Now().Before(binding.Spec.ExpiresAt.Time)) {
			continue
		}
		for _, name := range binding.Spec.PolicySets {
			for _, set := range snapshot.Sets {
				if set.Name != name || set.Namespace != binding.Namespace {
					continue
				}
				for _, rule := range set.Spec.Rules {
					// Unsupported globs fail closed, rather than silently broadening a rule.
					if rule.Type != "egress" || !slices.Contains(rule.Hosts, req.URL.Hostname()) {
						continue
					}
					if !connect && ((len(rule.Methods) != 0 && !slices.Contains(rule.Methods, req.Method)) || (len(rule.Paths) != 0 && !slices.Contains(rule.Paths, req.URL.Path))) {
						continue
					}
					if !connect && !credentialMatches(req, rule, snapshot.Credentials) {
						continue
					}
					return rule, snapshot, nil
				}
			}
		}
	}
	return policyapi.Rule{}, snapshot, errors.New("no matching rule")
}

func credentialMatches(req *http.Request, rule policyapi.Rule, credentials map[string]HeaderCredential) bool {
	wanted := ""
	for name, credential := range credentials {
		if slices.Contains(req.Header.Values(credential.Header), "agentplane-credential-"+name) {
			if wanted != "" && wanted != name {
				return false
			}
			wanted = name
		}
	}
	return wanted == "" || (rule.CredentialRef != nil && rule.CredentialRef.Name == wanted)
}

func substitute(req *http.Request, rule policyapi.Rule, credentials map[string]HeaderCredential) error {
	for name, credential := range credentials {
		values := req.Header.Values(credential.Header)
		for i, value := range values {
			if value != "agentplane-credential-"+name {
				continue
			}
			if rule.CredentialRef == nil || rule.CredentialRef.Name != name || credential.Value == "" {
				return errors.New("credential unavailable")
			}
			values[i] = credential.Value
		}
	}
	return nil
}

func sameRule(a, b policyapi.Rule) bool {
	return a.Type == b.Type && a.ClusterInternal == b.ClusterInternal && slices.Equal(a.Hosts, b.Hosts) && slices.Equal(a.Methods, b.Methods) && slices.Equal(a.Paths, b.Paths) &&
		((a.CredentialRef == nil && b.CredentialRef == nil) || (a.CredentialRef != nil && b.CredentialRef != nil && a.CredentialRef.Name == b.CredentialRef.Name))
}

func sameCredentials(a, b map[string]HeaderCredential) bool {
	if len(a) != len(b) {
		return false
	}
	for key, value := range a {
		if b[key] != value {
			return false
		}
	}
	return true
}

func pin(ctx context.Context, host string, internal bool, options Options) (netip.Addr, error) {
	addresses, err := options.Resolve(ctx, "ip", host)
	if err != nil || len(addresses) == 0 {
		return netip.Addr{}, errors.New("unresolved")
	}
	for _, address := range addresses {
		address = address.Unmap()
		exempt := slices.ContainsFunc(options.Exempt, func(prefix netip.Prefix) bool { return prefix.Contains(address) })
		// Bound the spike to IPv4 until the complete IPv6 special-purpose range
		// contract has coverage. Reserved/documentation/benchmark ranges never pass
		// merely because netip calls them global unicast.
		forbidden := !address.Is4() || !address.IsGlobalUnicast() || address.IsLoopback() || address.IsLinkLocalUnicast() || (!internal && address.IsPrivate())
		for _, reserved := range []string{"0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"} {
			forbidden = forbidden || netip.MustParsePrefix(reserved).Contains(address)
		}
		if forbidden && !exempt {
			return netip.Addr{}, errors.New("forbidden address")
		}
	}
	return addresses[0].Unmap(), nil
}

func deny(w http.ResponseWriter, reason string) {
	w.Header().Set("x-agentplane-egress", "denied; reason="+reason)
	w.WriteHeader(http.StatusForbidden)
}
