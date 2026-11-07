package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/observability"
)

const (
	refusedHeader        = "x-agentplane-egress-sidecar"
	projectedTokenHeader = "X-Agentplane-Workload-Token"
	readinessPath        = "/readyz"
	maxHeadBytes         = 64 * 1024
)

type sidecar struct {
	config   config
	proxyHop *proxyHop
	ready    atomic.Bool

	readinessServer   *http.Server
	readinessListener net.Listener
	proxyServer       *http.Server
	proxyListener     net.Listener
	proxyTransport    *http.Transport
	routes            *platformRoutes

	activeMu sync.Mutex
	active   map[net.Conn]struct{}
}

func (s *sidecar) run(ctx context.Context) error {
	readinessListener, err := netlistener.Open(s.config.ReadinessListenAddress)
	if err != nil {
		return fmt.Errorf("start readiness listener: %w", err)
	}

	routes, err := bindPlatformRoutes(s.config.PlatformRoutes)
	if err != nil {
		_ = readinessListener.Close()
		return err
	}

	proxyListener, err := netlistener.Open(s.config.ListenAddress)
	if err != nil {
		_ = readinessListener.Close()
		closePlatformRoutes(routes)
		return fmt.Errorf("start HTTP proxy listener: %w", err)
	}
	return s.serve(ctx, proxyListener, readinessListener, routes)
}

func (s *sidecar) serve(ctx context.Context, proxyListener, readinessListener net.Listener, routes *platformRoutes) error {
	s.proxyListener = proxyListener
	s.proxyServer = &http.Server{Handler: s.proxyHandler(), MaxHeaderBytes: maxHeadBytes}
	s.routes = routes
	s.bindReadiness(readinessListener)
	var serving sync.WaitGroup
	defer func() {
		s.stopProxy()
		closePlatformRoutes(s.routes)
		s.stopReadiness()
		serving.Wait()
	}()

	if ctx.Err() != nil {
		return nil
	}
	serveErrors := make(chan error, 3)
	serve := func(name string, run func() error) {
		serving.Go(func() {
			if err := run(); err != nil {
				if errors.Is(err, http.ErrServerClosed) {
					return
				}
				serveErrors <- fmt.Errorf("%s stopped: %w", name, err)
			} else {
				serveErrors <- fmt.Errorf("%s stopped unexpectedly", name)
			}
		})
	}
	serve("readiness listener", func() error { return s.readinessServer.Serve(s.readinessListener) })
	if routes != nil {
		serve("platform routes", func() error { return routes.server.Serve(routes.listen) })
		slog.Info("platform routes listening", "address", routes.listen.Addr(), "route_count", len(s.config.PlatformRoutes.Routes))
	}
	serve("HTTP proxy listener", func() error {
		return s.proxyServer.Serve(&trackingListener{Listener: proxyListener, sidecar: s})
	})
	s.ready.Store(true)
	slog.Info("relaying HTTP proxy traffic", "listen", proxyListener.Addr(), "upstream", s.config.Proxy.Address)

	select {
	case <-ctx.Done():
		return nil
	case err := <-serveErrors:
		if ctx.Err() != nil {
			return nil
		}
		return err
	}
}

func (s *sidecar) bindReadiness(listener net.Listener) {
	s.readinessListener = listener
	s.readinessServer = &http.Server{
		Handler: observability.HTTPHandler(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
			if request.URL.Path != readinessPath {
				http.NotFound(writer, request)
				return
			}
			if request.Method != http.MethodGet && request.Method != http.MethodHead {
				writer.Header().Set("Allow", http.MethodGet)
				http.Error(writer, "method not allowed", http.StatusMethodNotAllowed)
				return
			}
			ready := s.ready.Load()
			writer.Header().Set("Content-Type", "application/json; charset=utf-8")
			writer.WriteHeader(map[bool]int{true: http.StatusOK, false: http.StatusServiceUnavailable}[ready])
			if ready && request.Method != http.MethodHead {
				_, _ = writer.Write([]byte(`{"ready":true}`))
			} else if !ready && request.Method != http.MethodHead {
				_, _ = writer.Write([]byte(`{"ready":false}`))
			}
		})),
	}
}

func (s *sidecar) stopReadiness() {
	if s.readinessServer == nil {
		return
	}
	s.ready.Store(false)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := s.readinessServer.Shutdown(ctx); err != nil {
		slog.Warn("readiness shutdown did not drain", "error", err)
		_ = s.readinessServer.Close()
	}
	_ = s.readinessListener.Close()
}

func (s *sidecar) stopProxy() {
	s.ready.Store(false)
	if s.proxyServer != nil {
		_ = s.proxyServer.Close()
	}
	if s.proxyTransport != nil {
		s.proxyTransport.CloseIdleConnections()
	}
	if s.proxyListener != nil {
		_ = s.proxyListener.Close()
	}
	s.activeMu.Lock()
	connections := make([]net.Conn, 0, len(s.active))
	for conn := range s.active {
		connections = append(connections, conn)
		delete(s.active, conn)
	}
	s.activeMu.Unlock()
	for _, conn := range connections {
		_ = conn.Close()
	}
}

type trackingListener struct {
	net.Listener
	sidecar *sidecar
}

func (l *trackingListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	tracked := &trackedClientConn{Conn: conn, sidecar: l.sidecar}
	l.sidecar.track(tracked, true)
	return tracked, nil
}

type trackedClientConn struct {
	net.Conn
	sidecar *sidecar
}

func (c *trackedClientConn) Close() error {
	c.sidecar.track(c, false)
	return c.Conn.Close()
}

func (c *trackedClientConn) CloseWrite() error {
	if closer, ok := c.Conn.(interface{ CloseWrite() error }); ok {
		return closer.CloseWrite()
	}
	return c.Conn.Close()
}

func (s *sidecar) proxyHandler() http.Handler {
	proxyScheme := "http"
	if _, usesTLS := s.proxyHop.transport.(tlsProxyTransport); usesTLS {
		proxyScheme = "https"
	}
	proxyURL := &url.URL{Scheme: proxyScheme, Host: s.proxyHop.address}
	dialHop := func(ctx context.Context, network, address string) (net.Conn, error) {
		if network != "tcp" || address != s.proxyHop.address {
			return nil, errors.New("unexpected central proxy destination")
		}
		return s.proxyHop.transport.dial(ctx, address)
	}
	transport := &http.Transport{
		Proxy:                 http.ProxyURL(proxyURL),
		DialContext:           dialHop,
		IdleConnTimeout:       time.Minute,
		ExpectContinueTimeout: time.Second,
	}
	if proxyScheme == "https" {
		// DialTLSContext returns an already-handshaken connection so the SVID expiry
		// watcher and the transport use the same authenticated hop as CONNECT routes.
		transport.DialTLSContext = dialHop
	}
	s.proxyTransport = transport
	proxy := &httputil.ReverseProxy{
		Transport: transport,
		Rewrite: func(request *httputil.ProxyRequest) {
			request.Out.RequestURI = ""
			token := request.In.Context().Value(proxyTokenKey{}).(string)
			rewriteRequest(request.Out, token, s.readProjectedTokens())
		},
		ErrorHandler: func(writer http.ResponseWriter, _ *http.Request, err error) {
			slog.Warn("central proxy request failed", "address", s.config.Proxy.Address, "error", err)
			refuse(writer, http.StatusBadGateway, "proxy-unreachable")
		},
	}
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method == http.MethodConnect {
			s.handleConnect(writer, request)
			return
		}
		if request.URL.Scheme == "" && request.URL.Host == "" && request.Host != "" {
			// Accept origin-form requests from local clients as requests to their Host.
			request = request.Clone(request.Context())
			request.URL.Scheme = "http"
			request.URL.Host = request.Host
		} else if request.URL.Scheme != "http" || request.URL.Host == "" {
			refuse(writer, http.StatusBadRequest, "bad-request")
			return
		}
		token, err := s.proxyHop.auth.requestToken()
		if err != nil {
			slog.Error("central proxy credential unavailable", "address", s.proxyHop.address, "error", err)
			refuse(writer, http.StatusServiceUnavailable, "token-unavailable")
			return
		}
		proxy.ServeHTTP(writer, request.WithContext(context.WithValue(request.Context(), proxyTokenKey{}, token)))
	})
}

type proxyTokenKey struct{}

func (s *sidecar) handleConnect(writer http.ResponseWriter, request *http.Request) {
	authority := request.URL.Host
	if authority == "" {
		authority = request.Host
	}
	if authority == "" {
		refuse(writer, http.StatusBadRequest, "bad-request")
		return
	}
	token, err := s.proxyHop.auth.requestToken()
	if err != nil {
		slog.Error("central proxy credential unavailable", "address", s.proxyHop.address, "error", err)
		refuse(writer, http.StatusServiceUnavailable, "token-unavailable")
		return
	}
	upstream, err := s.proxyHop.transport.dial(request.Context(), s.proxyHop.address)
	if err != nil {
		slog.Warn("central proxy unreachable", "address", s.proxyHop.address, "error", err)
		refuse(writer, http.StatusBadGateway, "proxy-unreachable")
		return
	}
	s.track(upstream, true)
	defer func() {
		s.track(upstream, false)
		_ = upstream.Close()
	}()

	upstreamRequest := request.Clone(request.Context())
	upstreamRequest.RequestURI = ""
	upstreamRequest.URL = &url.URL{Host: authority}
	upstreamRequest.Host = authority
	upstreamRequest.Body = nil
	rewriteRequest(upstreamRequest, token, s.readProjectedTokens())
	if err := upstreamRequest.WriteProxy(upstream); err != nil {
		slog.Warn("writing CONNECT to central proxy failed", "error", err)
		refuse(writer, http.StatusBadGateway, "proxy-unreachable")
		return
	}
	upstreamReader := bufio.NewReader(upstream)
	response, err := http.ReadResponse(upstreamReader, upstreamRequest)
	if err != nil {
		slog.Warn("reading CONNECT response from central proxy failed", "error", err)
		refuse(writer, http.StatusBadGateway, "proxy-unreachable")
		return
	}
	if response.StatusCode/100 != 2 {
		defer response.Body.Close()
		for name, values := range response.Header {
			for _, value := range values {
				writer.Header().Add(name, value)
			}
		}
		writer.Header().Set("Connection", "close")
		writer.WriteHeader(response.StatusCode)
		if _, err := io.Copy(writer, response.Body); err != nil {
			slog.Warn("relaying CONNECT refusal from central proxy failed", "error", err)
		}
		return
	}
	_ = response.Body.Close()
	client, buffered, err := http.NewResponseController(writer).Hijack()
	if err != nil {
		slog.Warn("hijacking CONNECT client failed", "error", err)
		return
	}
	defer client.Close()
	if err := writeResponseHead(client, response); err != nil {
		return
	}
	if err := pipeDuplex(client, buffered.Reader, upstream, upstreamReader); err != nil {
		slog.Warn("CONNECT relay ended with connection error", "address", s.config.Proxy.Address, "error", err)
	}
}

// rewriteRequest replaces caller-supplied proxy credentials with sidecar-owned credentials.
func rewriteRequest(request *http.Request, token string, projected map[string]string) {
	request.Header.Del("Proxy-Connection")
	request.Header.Del(projectedTokenHeader)
	request.Header.Del("Proxy-Authorization")
	if token != "" {
		request.Header.Set("Proxy-Authorization", "Bearer "+token)
	}
	for _, audience := range slices.Sorted(maps.Keys(projected)) {
		request.Header.Add(projectedTokenHeader, audience+" "+projected[audience])
	}
	if _, ok := request.Header["User-Agent"]; !ok {
		request.Header["User-Agent"] = []string{""}
	}
}

func refuse(writer http.ResponseWriter, status int, reason string) {
	writer.Header().Set(refusedHeader, "reason="+reason)
	writer.WriteHeader(status)
}

// writeResponseHead relays the response head before an opaque CONNECT tunnel.
func writeResponseHead(writer io.Writer, response *http.Response) error {
	if _, err := fmt.Fprintf(writer, "HTTP/%d.%d %s\r\n", response.ProtoMajor, response.ProtoMinor, response.Status); err != nil {
		return err
	}
	if err := response.Header.Write(writer); err != nil {
		return err
	}
	_, err := io.WriteString(writer, "\r\n")
	return err
}

func (s *sidecar) track(conn net.Conn, add bool) {
	s.activeMu.Lock()
	defer s.activeMu.Unlock()
	if s.active == nil {
		s.active = make(map[net.Conn]struct{})
	}
	if add {
		s.active[conn] = struct{}{}
	} else {
		delete(s.active, conn)
	}
}

func (s *sidecar) readProjectedTokens() map[string]string {
	tokens := make(map[string]string, len(s.config.AudienceTokenFiles))
	for audience, path := range s.config.AudienceTokenFiles {
		if token, err := readToken(path); err == nil {
			tokens[audience] = token
		} else {
			slog.Warn("projected audience token unavailable", "audience", audience, "path", path, "error", err)
		}
	}
	return tokens
}

func pipeDuplex(client net.Conn, clientReader *bufio.Reader, upstream net.Conn, upstreamReader *bufio.Reader) error {
	results := make(chan error, 2)
	go func() {
		_, err := io.Copy(upstream, clientReader)
		if closer, okay := upstream.(interface{ CloseWrite() error }); okay {
			_ = closer.CloseWrite()
		}
		results <- err
	}()
	go func() {
		_, err := io.Copy(client, upstreamReader)
		if closer, okay := client.(interface{ CloseWrite() error }); okay {
			_ = closer.CloseWrite()
		}
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
