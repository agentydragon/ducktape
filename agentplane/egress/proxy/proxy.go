// Package proxy is the central egress proxy: CONNECT tunnels, intercepted with a forged leaf, and
// plain HTTP proxying, on net/http and httputil.ReverseProxy. Every request, inside a tunnel or
// not, passes identity, policy, credential and address checks before anything is dialed; a failure
// anywhere is a refusal, never a forward.
//
// The checks see the authority a request names (the CONNECT target for the CONNECT itself, the
// request's own authority for what runs inside), and the dial goes to exactly the address the check
// pinned for that authority.
package proxy

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"log"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/presentation"
	"github.com/agentydragon/ducktape/agentplane/egress/upstream"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/google/uuid"
)

// Options is everything the proxy needs to run; none of it has a default.
type Options struct {
	// CA signs the leaf certificates the sandboxes' clients are shown inside a tunnel.
	CA tls.Certificate
	// UpstreamRoots verifies the destinations, independently of CA.
	UpstreamRoots *x509.CertPool
	Identity      *identity.Verifier
	Policy        *policy.Index
	Upstream      *upstream.Resolver
	// Record receives every decision. It must not block.
	Record func(decision.Record)
}

// Proxy serves egress requests until Drain.
type Proxy struct {
	options  Options
	leaves   *leafIssuer
	producer uuid.UUID
	flights  *flights
	tunnels  tunnels
	server   *http.Server
}

// New returns a Proxy. The interception CA must be able to sign.
func New(options Options) (*Proxy, error) {
	if len(options.CA.Certificate) == 0 || options.CA.PrivateKey == nil || options.UpstreamRoots == nil ||
		options.Identity == nil || options.Policy == nil || options.Upstream == nil || options.Record == nil {
		return nil, errors.New("interception CA, upstream roots, identity, policy, upstream resolver and record are required")
	}
	leaves, err := newLeafIssuer(options.CA)
	if err != nil {
		return nil, err
	}
	p := &Proxy{options: options, leaves: leaves, producer: uuid.New(), flights: newFlights()}
	p.server = &http.Server{
		Handler: observability.HTTPHandler(http.HandlerFunc(p.serve)), ReadHeaderTimeout: 30 * time.Second, ErrorLog: log.New(io.Discard, "", 0),
		ConnContext: func(ctx context.Context, _ net.Conn) context.Context {
			return context.WithValue(ctx, connectionIDKey{}, uuid.NewString())
		},
	}
	return p, nil
}

// Serve accepts proxy connections on listener until Drain, when it returns http.ErrServerClosed.
func (p *Proxy) Serve(listener net.Listener) error { return p.server.Serve(listener) }

// Drain stops the proxy admitting anything new and lets the requests in flight finish and their
// responses reach the client. It returns once they have, or ctx ends, and either way closes every
// tunnel before it returns: a tunnel with nothing in flight does not delay it.
func (p *Proxy) Drain(ctx context.Context) error {
	p.options.Policy.Health.MarkDraining()
	// Shutdown closes the listeners first; requests inside tunnels are invisible to it, so they are
	// waited for separately, and then each tunnel is shut down to let what they answered be written.
	err := errors.Join(p.server.Shutdown(ctx), p.flights.wait(ctx), p.tunnels.shutdown(ctx))
	p.tunnels.closeAll()
	return errors.Join(err, p.server.Close())
}

// connectionIDKey carries the identifier of the client connection a request arrived on.
type connectionIDKey struct{}

// connection is the authenticated state of one client connection, which a tunnel's requests share:
// their headers are behind the tunnel's TLS, so what the CONNECT presented is what applies.
type connection struct {
	id string

	mu   sync.Mutex
	auth *authenticated
}

// newConnection starts the state of a request that arrived on the accepted connection, whose
// identifier the decisions it leads to share.
func newConnection(r *http.Request) *connection {
	return &connection{id: r.Context().Value(connectionIDKey{}).(string)}
}

func (p *Proxy) serve(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodConnect:
		p.connect(w, r)
	case r.URL.IsAbs():
		// Only the identifier is shared along the client connection: a plain request proves its own
		// bearer, so one without inherits nothing.
		p.forward(w, r, newConnection(r))
	default:
		http.Error(w, "proxy requests only", http.StatusBadRequest)
	}
}

func (p *Proxy) connect(w http.ResponseWriter, r *http.Request) {
	host, port, err := net.SplitHostPort(r.Host)
	portNumber, portErr := strconv.ParseUint(port, 10, 16)
	if err != nil || portErr != nil || portNumber == 0 || host == "" {
		http.Error(w, "CONNECT needs host:port", http.StatusBadRequest)
		return
	}
	client := newConnection(r)
	verdict, _ := p.gate(r, client, nil, host, int(portNumber))
	if denied, ok := verdict.(policy.Denied); ok {
		deny(w, denied.Reason)
		return
	}
	conn, buffered, err := http.NewResponseController(w).Hijack()
	if err != nil {
		http.Error(w, "CONNECT needs HTTP/1.1", http.StatusHTTPVersionNotSupported)
		return
	}
	defer conn.Close()
	if !p.tunnels.add(conn) {
		return
	}
	defer p.tunnels.remove(conn)
	// Acknowledge without contacting the upstream. Each request inside is admitted on its own, so a
	// refused one dials nothing.
	if _, err := io.WriteString(conn, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		return
	}
	p.serveTunnel(conn, buffered.Reader, r.Host, client)
}

// serveTunnel serves one CONNECT tunnel: TLS (HTTP/1.1 or h2 by ALPN) intercepted with a forged
// leaf, or cleartext HTTP/1.1 or prior-knowledge h2c.
func (p *Proxy) serveTunnel(conn net.Conn, buffered *bufio.Reader, authority string, client *connection) {
	if err := conn.SetReadDeadline(time.Now().Add(30 * time.Second)); err != nil {
		return
	}
	first, err := buffered.Peek(1)
	if err != nil || conn.SetReadDeadline(time.Time{}) != nil {
		return
	}
	scheme := "http"
	// The close notification sits below TLS so the server is handed the *tls.Conn itself and
	// negotiates h2 from its ALPN result.
	closed := make(chan struct{})
	var tunneled net.Conn = &closeNotifyConn{Conn: &readBufferedConn{Conn: conn, reader: buffered}, closed: closed}
	if first[0] == tlsRecordHandshake {
		scheme = "https"
		fallbackName, _, _ := net.SplitHostPort(authority)
		secured := tls.Server(tunneled, &tls.Config{
			MinVersion: tls.VersionTLS12, NextProtos: []string{"h2", "http/1.1"},
			GetCertificate: func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
				if hello.ServerName != "" {
					return p.leaves.issue(hello.ServerName)
				}
				return p.leaves.issue(fallbackName)
			},
		})
		handshake, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := secured.HandshakeContext(handshake); err != nil {
			slog.Warn("tunnel TLS handshake failed", "error", err)
			return
		}
		tunneled = secured
	}
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetHTTP2(true)
	protocols.SetUnencryptedHTTP2(true)
	inner := &http.Server{
		Protocols: protocols, ErrorLog: log.New(io.Discard, "", 0), ReadHeaderTimeout: 30 * time.Second,
		Handler: observability.HTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			if req.Method == http.MethodConnect {
				http.Error(w, "CONNECT inside a tunnel", http.StatusMethodNotAllowed)
				return
			}
			// The inner authority is authoritative: it is what policy and the pinned dial see.
			host := req.Host
			if host == "" {
				host = authority
			}
			req.URL.Scheme, req.URL.Host = scheme, host
			p.forward(w, req, client)
		})),
	}
	p.tunnels.serving(conn, inner)
	// http.Server serves one accepted connection and returns when it ends, or when Drain shuts the
	// server down; the connection itself ends later in that case, once its responses are written.
	_ = inner.Serve(newSingleConnListener(tunneled, closed))
	<-closed
}

// forward gates one proxied request (target in req.URL) and relays it only if every gate passes.
func (p *Proxy) forward(w http.ResponseWriter, req *http.Request, client *connection) {
	// Counted before the gate: a request is in flight from the moment it can be admitted, so a drain
	// that has started waiting never misses one that is about to be.
	defer p.flights.begin()()
	host, port := req.URL.Hostname(), req.URL.Port()
	if port == "" {
		port = map[string]string{"http": "80", "https": "443"}[req.URL.Scheme]
	}
	portNumber, err := strconv.ParseUint(port, 10, 16)
	if err != nil || portNumber == 0 {
		http.Error(w, "invalid port", http.StatusBadRequest)
		return
	}
	body, err := readEnvelope(req)
	if err != nil {
		slog.Info("request body unreadable", "error", err)
		http.Error(w, "request body", http.StatusBadRequest)
		return
	}
	verdict, pin := p.gate(req, client, body, host, int(portNumber))
	switch v := verdict.(type) {
	case policy.Denied:
		deny(w, v.Reason)
	case policy.Allowed:
		p.relay(w, req, v, pin, net.JoinHostPort(host, port))
	}
}

// readEnvelope reads the start of a request body that a JSON credential could be presented in, and
// leaves req.Body to replay it. It is nil for every other request, whose body the decision never
// reads and which therefore streams.
func readEnvelope(req *http.Request) ([]byte, error) {
	if !presentation.IsJSONEnvelope(req.Header) {
		return nil, nil
	}
	if req.Body == nil {
		return []byte{}, nil
	}
	head, err := io.ReadAll(io.LimitReader(req.Body, presentation.MaxJSONBodyBytes+1))
	if err != nil {
		return nil, err
	}
	req.Body = &replayedBody{Reader: io.MultiReader(bytes.NewReader(head), req.Body), Closer: req.Body}
	return head, nil
}

type replayedBody struct {
	io.Reader
	io.Closer
}

// relay forwards an admitted request to the address the gate pinned for target, with the
// credentials the decision substituted.
func (p *Proxy) relay(w http.ResponseWriter, req *http.Request, admitted policy.Allowed, pin upstream.Pin, target string) {
	for _, rewrite := range admitted.Rewrites {
		rewrite.Apply(req.Header)
	}
	if admitted.BodyRewrite != nil {
		req.Body = io.NopCloser(bytes.NewReader(admitted.BodyRewrite.Content))
		req.ContentLength = int64(len(admitted.BodyRewrite.Content))
		req.TransferEncoding = nil
	}
	dialed := net.JoinHostPort(pin.Address.Unmap().String(), strconv.Itoa(pin.Port))
	transport := &http.Transport{
		Proxy: nil, DisableKeepAlives: true, DisableCompression: true,
		ForceAttemptHTTP2: req.Header.Get("Upgrade") == "",
		TLSClientConfig:   &tls.Config{RootCAs: p.options.UpstreamRoots, MinVersion: tls.VersionTLS12},
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			// The transport lowercases the host it dials; the target was admitted as written.
			if !strings.EqualFold(address, target) {
				return nil, errors.New("unadmitted dial target")
			}
			var dialer net.Dialer
			return dialer.DialContext(ctx, network, dialed)
		},
	}
	// A normal http.Transport speaks HTTP/1 to plaintext upstreams. h2c needs prior-knowledge HTTP/2
	// selected explicitly, with HTTP/1 off.
	if req.URL.Scheme == "http" && req.ProtoMajor == 2 {
		transport.Protocols = new(http.Protocols)
		transport.Protocols.SetUnencryptedHTTP2(true)
	}
	authority := req.URL.Host
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
