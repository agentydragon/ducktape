package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/textproto"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
)

// A workload route names one HTTP origin the workload already uses. Only the
// credential-holding sidecar originates the destination TLS connection; the
// central proxy sees an authenticated CONNECT and opaque service TLS bytes.
type workloadRouteConfig struct {
	Upstream  string                               `yaml:"upstream"`
	ServerURI string                               `yaml:"server_uri"`
	TLS       environment_runtime_transport.Config `yaml:"tls"`
}

type workloadRoute struct {
	config    workloadRouteConfig
	security  *tls.Config
	transport *http.Transport
}

func (s *sidecar) prepareWorkloadRoutes() error {
	s.workloadRoutes = make(map[string]*workloadRoute, len(s.config.WorkloadRoutes))
	for authority, config := range s.config.WorkloadRoutes {
		if err := validateWorkloadRoute(authority, config); err != nil {
			return err
		}
		security, err := config.TLS.ClientTLSConfig(config.ServerURI)
		if err != nil {
			return fmt.Errorf("workload route %q: %w", authority, err)
		}
		security.NextProtos = []string{"h2", "http/1.1"}
		route := &workloadRoute{config: config, security: security}
		route.transport = &http.Transport{
			Proxy: nil, ForceAttemptHTTP2: true, TLSClientConfig: security,
			TLSHandshakeTimeout:   10 * time.Second,
			ExpectContinueTimeout: time.Second, IdleConnTimeout: time.Minute,
			DialTLSContext: func(ctx context.Context, network, address string) (net.Conn, error) {
				if network != "tcp" || address != config.Upstream {
					return nil, errors.New("unexpected workload route destination")
				}
				return s.dialWorkloadRoute(ctx, config.Upstream, security)
			},
		}
		s.workloadRoutes[authority] = route
	}
	return nil
}

func validateWorkloadRoute(authority string, config workloadRouteConfig) error {
	for _, address := range []string{authority, config.Upstream} {
		host, port, err := net.SplitHostPort(address)
		portNumber, portError := strconv.Atoi(port)
		if err != nil || host == "" || portError != nil || portNumber < 1 || portNumber > 65535 || strings.ContainsAny(address, "/\\?#@") {
			return fmt.Errorf("workload route requires exact host:port: %q", address)
		}
	}
	if config.ServerURI == "" {
		return errors.New("workload route server_uri is required")
	}
	return config.TLS.Validate()
}

func (s *sidecar) closeWorkloadRoutes() {
	for _, route := range s.workloadRoutes {
		route.transport.CloseIdleConnections()
	}
}

func (s *sidecar) dialWorkloadRoute(ctx context.Context, target string, security *tls.Config) (net.Conn, error) {
	connection, err := s.dialProxy()
	if err != nil {
		return nil, err
	}
	complete := false
	defer func() {
		if !complete {
			connection.Close()
		}
	}()
	// Bound CONNECT and both handshakes even when the caller has no deadline.
	deadline := time.Now().Add(10 * time.Second)
	if callerDeadline, ok := ctx.Deadline(); ok && callerDeadline.Before(deadline) {
		deadline = callerDeadline
	}
	if err := connection.SetDeadline(deadline); err != nil {
		return nil, err
	}
	stop := context.AfterFunc(ctx, func() { connection.Close() })
	defer stop()
	request := &http.Request{Method: http.MethodConnect, URL: &url.URL{Host: target}, Host: target, Header: make(http.Header)}
	token, err := s.proxyHop.auth.requestToken()
	if err != nil {
		return nil, fmt.Errorf("load proxy hop credential: %w", err)
	}
	rewriteRequest(request, token, nil)
	if err := request.WriteProxy(connection); err != nil {
		return nil, err
	}
	reader := bufio.NewReader(connection)
	response, err := http.ReadResponse(reader, request)
	if err != nil {
		return nil, err
	}
	if response.StatusCode != http.StatusOK {
		response.Body.Close()
		return nil, fmt.Errorf("central proxy refused workload CONNECT: %s", response.Status)
	}
	tracked := &trackedWorkloadConnection{Conn: &bufferedWorkloadConnection{Conn: connection, reader: reader}, sidecar: s}
	configuration := security.Clone()
	getCertificate := configuration.GetClientCertificate
	if getCertificate == nil {
		return nil, errors.New("workload route requires a rotating client certificate source")
	}
	var localNotAfter time.Time
	configuration.GetClientCertificate = func(info *tls.CertificateRequestInfo) (*tls.Certificate, error) {
		certificate, err := getCertificate(info)
		if err != nil {
			return nil, err
		}
		if certificate == nil || len(certificate.Certificate) == 0 {
			return nil, errors.New("workload route selected no client leaf")
		}
		leaf, err := x509.ParseCertificate(certificate.Certificate[0])
		if err != nil {
			return nil, err
		}
		localNotAfter = leaf.NotAfter
		return certificate, nil
	}
	inner := tls.Client(tracked, configuration)
	if err := inner.HandshakeContext(ctx); err != nil {
		return nil, fmt.Errorf("workload service TLS: %w", err)
	}
	if err := connection.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	s.track(tracked, true)
	tracked.setExpiry(environment_runtime_transport.CloseTLSConnAtCertificateExpiry(inner, localNotAfter))
	complete = true
	return inner, nil
}

// exchangeWorkloadConnectRoute carries native h2c gRPC frames through the caller's existing HTTP
// CONNECT authority. The sidecar adds destination TLS and its Runner SVID after central admission;
// the central proxy sees only opaque service TLS bytes.
func (s *sidecar) exchangeWorkloadConnectRoute(client net.Conn, reader *bufio.Reader, route *workloadRoute) {
	ctx, cancel := context.WithTimeout(s.workloadContext, 10*time.Second)
	defer cancel()
	upstream, err := s.dialWorkloadRoute(ctx, route.config.Upstream, route.security)
	if err != nil {
		slog.Warn("workload CONNECT route refused", "upstream", route.config.Upstream, "error", err)
		writeRefusal(client, http.StatusBadGateway, "workload-tls-unavailable")
		return
	}
	defer upstream.Close()
	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		return
	}
	if err := pipeWorkloadTunnel(client, reader, upstream); err != nil {
		slog.Warn("workload CONNECT route ended with connection error", "upstream", route.config.Upstream, "error", err)
	}
}

func pipeWorkloadTunnel(client net.Conn, reader *bufio.Reader, upstream net.Conn) error {
	results := make(chan error, 2)
	go func() {
		_, err := io.Copy(upstream, reader)
		if closeWriter, okay := upstream.(interface{ CloseWrite() error }); okay {
			_ = closeWriter.CloseWrite()
		}
		results <- err
	}()
	go func() {
		_, err := io.Copy(client, upstream)
		_ = client.Close()
		results <- err
	}()
	first := <-results
	if first != nil {
		_ = client.Close()
		_ = upstream.Close()
	}
	second := <-results
	if first != nil {
		return first
	}
	return second
}

type bufferedWorkloadConnection struct {
	net.Conn
	reader *bufio.Reader
}

func (c *bufferedWorkloadConnection) Read(p []byte) (int, error) { return c.reader.Read(p) }

type trackedWorkloadConnection struct {
	net.Conn
	sidecar      *sidecar
	mu           sync.Mutex
	closed       bool
	cancelExpiry func()
}

func (c *trackedWorkloadConnection) Close() error {
	c.mu.Lock()
	c.closed = true
	if c.cancelExpiry != nil {
		c.cancelExpiry()
	}
	c.mu.Unlock()
	c.sidecar.track(c, false)
	return c.Conn.Close()
}

func (c *trackedWorkloadConnection) setExpiry(cancel func()) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		cancel()
		return
	}
	c.cancelExpiry = cancel
}

// exchangeWorkloadRoute uses the normal HTTP transport for body framing,
// cancellation, informational responses and streaming responses. The route is
// selected by an exact configured authority before any credential substitution.
func (s *sidecar) exchangeWorkloadRoute(client net.Conn, reader *bufio.Reader, request *http.Request, route *workloadRoute) bool {
	ctx, cancel := context.WithCancel(s.workloadContext)
	defer cancel()
	outgoing := request.Clone(ctx)
	outgoing.RequestURI = ""
	outgoing.URL.Scheme = "https"
	outgoing.URL.Host = route.config.Upstream
	outgoing.Host = route.config.Upstream
	outgoing.Header.Del("Authorization")
	outgoing.Header.Del("Proxy-Authorization")
	outgoing.Header.Del("Proxy-Connection")
	outgoing.Header.Del(projectedTokenHeader)
	// net/http forwards an upstream 100 Continue before consuming a waiting
	// client's body. Other informational responses retain their status/headers.
	trace := &httptrace.ClientTrace{Got1xxResponse: func(code int, header textproto.MIMEHeader) error {
		return (&http.Response{StatusCode: code, ProtoMajor: 1, ProtoMinor: 1, Header: http.Header(header)}).Write(client)
	}}
	outgoing = outgoing.WithContext(httptrace.WithClientTrace(outgoing.Context(), trace))
	response, err := route.transport.RoundTrip(outgoing)
	if err != nil {
		slog.Warn("workload TLS route refused", "upstream", route.config.Upstream, "error", err)
		writeRefusal(client, http.StatusBadGateway, "workload-tls-unavailable")
		return false
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusSwitchingProtocols {
		upgraded, ok := response.Body.(io.ReadWriteCloser)
		if !ok {
			writeRefusal(client, http.StatusBadGateway, "workload-upgrade-unavailable")
			return false
		}
		if err := writeResponseHead(client, response); err != nil {
			return false
		}
		ended := make(chan struct{}, 2)
		go func() { io.Copy(upgraded, reader); ended <- struct{}{} }()
		go func() { io.Copy(client, upgraded); ended <- struct{}{} }()
		<-ended
		upgraded.Close()
		client.Close()
		<-ended
		return false
	}
	// A proxy response to an HTTP/1 client must use its framing even if the
	// service negotiated HTTP/2 on the private destination connection.
	response.ProtoMajor = 1
	response.ProtoMinor = 1
	response.Proto = "HTTP/1.1"
	if response.ContentLength < 0 {
		if request.Method == http.MethodHead || response.StatusCode == http.StatusNoContent || response.StatusCode == http.StatusNotModified {
			response.Close = true
		} else {
			// HTTP/2 has no Transfer-Encoding. Without an explicit HTTP/1 body
			// boundary, Response.Write chooses close-delimiting on a private copy,
			// leaving this connection open and clients waiting for EOF.
			response.TransferEncoding = []string{"chunked"}
		}
	}
	if err := response.Write(client); err != nil {
		return false
	}
	return !request.Close && !response.Close
}
