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
	workloadContext context.Context
	workloadRoutes  map[string]*workloadRoute
	config          config
	proxyHop        *proxyHop
	ready           atomic.Bool

	readinessServer   *http.Server
	readinessListener net.Listener
	proxyServer       *http.Server
	proxyListener     net.Listener
	proxyTransport    *http.Transport
	routes            []*platformRoute

	activeMu sync.Mutex
	active   map[net.Conn]struct{}
}

func (s *sidecar) run(ctx context.Context) error {
	if err := s.prepareWorkloadRoutes(); err != nil {
		return err
	}
	defer s.closeWorkloadRoutes()
	readinessListener, err := netlistener.Open(s.config.ReadinessListenAddress)
	if err != nil {
		return fmt.Errorf("start readiness listener: %w", err)
	}
	routes, err := s.bindPlatformRoutes(s.config.PlatformRoutes)
	if err != nil {
		_ = readinessListener.Close()
		return err
	}
	proxyListener, err := netlistener.Open(s.config.ListenAddress)
	if err != nil {
		_ = readinessListener.Close()
		stopPlatformRoutes(routes)
		return fmt.Errorf("start HTTP proxy listener: %w", err)
	}
	return s.serve(ctx, proxyListener, readinessListener, routes)
}

func (s *sidecar) serve(ctx context.Context, proxyListener, readinessListener net.Listener, routes []*platformRoute) error {
	s.workloadContext = ctx
	s.proxyListener = proxyListener
	s.routes = routes
	s.bindReadiness(readinessListener)
	s.proxyServer = &http.Server{Handler: s.proxyHandler(), MaxHeaderBytes: maxHeadBytes}
	var serving sync.WaitGroup
	defer func() {
		s.stopProxy()
		stopPlatformRoutes(s.routes)
		s.stopReadiness()
		serving.Wait()
	}()
	if ctx.Err() != nil {
		return nil
	}
	serveErrors := make(chan error, len(routes)+2)
	serve := func(name string, run func() error) {
		serving.Go(func() {
			if err := run(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				serveErrors <- fmt.Errorf("%s stopped: %w", name, err)
			} else {
				serveErrors <- fmt.Errorf("%s stopped unexpectedly", name)
			}
		})
	}
	serve("readiness listener", func() error { return s.readinessServer.Serve(s.readinessListener) })
	for _, route := range routes {
		serve("platform route "+route.name, func() error { return route.server.Serve(route.listen) })
		slog.Info("platform route listening", "route", route.name, "address", route.listen.Addr(), "upstream", route.config.Upstream)
	}
	serve("HTTP proxy listener", func() error { return s.proxyServer.Serve(&trackingListener{Listener: proxyListener, sidecar: s}) })
	s.ready.Store(true)
	slog.Info("relaying HTTP proxy traffic", "listen", proxyListener.Addr(), "upstream", s.proxyHop.address)
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
			if request.Method != http.MethodHead {
				if ready {
					_, _ = writer.Write([]byte(`{"ready":true}`))
				} else {
					_, _ = writer.Write([]byte(`{"ready":false}`))
				}
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
	connections := slices.Collect(maps.Keys(s.active))
	clear(s.active)
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
	if _, ok := s.proxyHop.transport.(tlsProxyTransport); ok {
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
		DialTLSContext:        dialHop,
		IdleConnTimeout:       time.Minute,
		ExpectContinueTimeout: time.Second,
	}
	s.proxyTransport = transport
	proxy := &httputil.ReverseProxy{
		Transport: transport,
		Rewrite: func(r *httputil.ProxyRequest) {
			r.Out.RequestURI = ""
			rewriteRequest(r.Out, r.In.Context().Value(proxyTokenKey{}).(string), s.readProjectedTokens())
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			slog.Warn("central proxy request failed", "address", s.proxyHop.address, "error", err)
			refuse(w, http.StatusBadGateway, "proxy-unreachable")
		},
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodConnect {
			s.handleConnect(w, r)
			return
		}
		if r.URL.Scheme == "" && r.URL.Host == "" && r.Host != "" {
			// Local clients may send origin-form requests to the proxy listener.
			// Only absolute-form requests select a privileged workload route.
			r = r.Clone(r.Context())
			r.URL.Scheme = "http"
			r.URL.Host = r.Host
		} else if r.URL.Scheme != "http" || r.URL.Host == "" {
			refuse(w, http.StatusBadRequest, "bad-request")
			return
		}
		authority := r.URL.Host
		if r.URL.Port() == "" {
			authority = net.JoinHostPort(r.URL.Hostname(), "80")
		}
		if absolute {
			if route := s.workloadRoutes[authority]; route != nil {
				s.exchangeWorkloadRoute(w, r, route)
				return
			}
		}
		token, err := s.proxyHop.auth.requestToken()
		if err != nil {
			slog.Warn("proxy hop credential unavailable", "error", err)
			refuse(w, http.StatusServiceUnavailable, "token-unavailable")
			return
		}
		proxy.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), proxyTokenKey{}, token)))
	})
}

type proxyTokenKey struct{}

func (s *sidecar) handleConnect(w http.ResponseWriter, r *http.Request) {
	authority := r.URL.Host
	if authority == "" {
		authority = r.Host
	}
	if route := s.workloadRoutes[authority]; route != nil {
		s.exchangeWorkloadConnectRoute(w, r, route)
		return
	}
	token, err := s.proxyHop.auth.requestToken()
	if err != nil {
		refuse(w, http.StatusServiceUnavailable, "token-unavailable")
		return
	}
	projected := s.readProjectedTokens()
	upstream, err := s.proxyHop.transport.dial(r.Context(), s.proxyHop.address)
	if err != nil {
		slog.Warn("central proxy unreachable", "address", s.proxyHop.address, "error", err)
		refuse(w, http.StatusBadGateway, "proxy-unreachable")
		return
	}
	defer upstream.Close()
	s.track(upstream, true)
	defer s.track(upstream, false)
	request := r.Clone(r.Context())
	request.RequestURI = ""
	request.URL = &url.URL{Host: authority}
	request.Host = authority
	request.Body = nil
	rewriteRequest(request, token, projected)
	if err := request.WriteProxy(upstream); err != nil {
		refuse(w, http.StatusBadGateway, "proxy-unreachable")
		return
	}
	reader := bufio.NewReader(upstream)
	response, err := http.ReadResponse(reader, request)
	if err != nil {
		refuse(w, http.StatusBadGateway, "proxy-unreachable")
		return
	}
	if response.StatusCode/100 != 2 {
		defer response.Body.Close()
		w.Header().Set("Connection", "close")
		for name, values := range response.Header {
			for _, value := range values {
				w.Header().Add(name, value)
			}
		}
		w.WriteHeader(response.StatusCode)
		_, _ = io.Copy(w, response.Body)
		return
	}
	client, buffered, err := http.NewResponseController(w).Hijack()
	if err != nil {
		return
	}
	defer client.Close()
	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		return
	}
	if err := pipeDuplex(client, buffered.Reader, upstream, reader); err != nil {
		slog.Warn("relay ended with connection error", "upstream", s.proxyHop.address, "error", err)
	}
}

// rewriteRequest replaces caller-supplied proxy credentials with sidecar-owned ones.
func rewriteRequest(request *http.Request, token string, projected map[string]string) {
	request.Header.Del("Proxy-Connection")
	request.Header.Del(projectedTokenHeader)
	if token == "" {
		request.Header.Del("Proxy-Authorization")
	} else {
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
		if closeWriter, okay := upstream.(interface{ CloseWrite() error }); okay {
			_ = closeWriter.CloseWrite()
		}
		results <- err
	}()
	go func() {
		_, err := io.Copy(client, upstreamReader)
		if closeWriter, okay := client.(interface{ CloseWrite() error }); okay {
			_ = closeWriter.CloseWrite()
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
