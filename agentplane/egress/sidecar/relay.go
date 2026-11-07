package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"math"
	"net"
	"net/http"
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
	pipeChunkBytes       = 64 * 1024
)

type sidecar struct {
	workloadContext context.Context
	workloadRoutes  map[string]*workloadRoute
	config          config
	proxyHop        *proxyHop
	ready           atomic.Bool

	readinessServer   *http.Server
	readinessListener net.Listener
	proxyListener     net.Listener
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

	routes, err := bindPlatformRoutes(s.config.PlatformRoutes)
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
			if err := run(); err != nil {
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
	serve("HTTP proxy listener", s.acceptLoop)
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

func (s *sidecar) acceptLoop() error {
	for {
		client, err := s.proxyListener.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return nil
			}
			return fmt.Errorf("accept HTTP proxy connection: %w", err)
		}
		s.track(client, true)
		go s.handle(client)
	}
}

// handle serves one workload connection over one central proxy connection of its own. Every
// request the workload sends on it, pipelined or not, is parsed and re-serialized through
// rewriteRequest; only a successful CONNECT or a 101 upgrade turns the rest of the connection into
// opaque bytes.
func (s *sidecar) handle(client net.Conn) {
	defer func() {
		s.track(client, false)
		_ = client.Close()
	}()
	// headLimit bounds each request head, as http.Server's MaxHeaderBytes does; bodies are unbounded.
	headLimit := &io.LimitedReader{R: client}
	clientReader := bufio.NewReaderSize(headLimit, pipeChunkBytes)
	var upstream net.Conn
	var upstreamReader *bufio.Reader
	defer func() {
		if upstream != nil {
			s.track(upstream, false)
			_ = upstream.Close()
		}
	}()
	for {
		headLimit.N = maxHeadBytes
		request, err := http.ReadRequest(clientReader)
		headLimit.N = math.MaxInt64
		if errors.Is(err, io.EOF) {
			return
		}
		if err != nil {
			slog.Warn("refusing malformed request", "error", err)
			writeRefusal(client, http.StatusBadRequest, "bad-request")
			return
		}
		if request.Method == http.MethodConnect {
			authority := request.URL.Host
			if authority == "" {
				authority = request.Host
			}
			if route := s.workloadRoutes[authority]; route != nil {
				s.exchangeWorkloadConnectRoute(client, clientReader, route)
				return
			}
		} else if request.URL.Scheme == "http" {
			authority := request.URL.Host
			if request.URL.Port() == "" {
				authority = net.JoinHostPort(request.URL.Hostname(), "80")
			}
			if route := s.workloadRoutes[authority]; route != nil {
				if !s.exchangeWorkloadRoute(client, clientReader, request, route) {
					return
				}
				continue
			}
		}
		token, err := s.proxyHop.auth.requestToken()
		if err != nil {
			slog.Warn("proxy hop credential unavailable", "error", err)
			writeRefusal(client, http.StatusServiceUnavailable, "token-unavailable")
			return
		}
		projected := s.readProjectedTokens()
		rewriteRequest(request, token, projected)
		if upstream == nil {
			upstream, err = s.dialProxy()
			if err != nil {
				slog.Warn("central proxy unreachable", "address", s.proxyHop.address, "error", err)
				writeRefusal(client, http.StatusBadGateway, "proxy-unreachable")
				return
			}
			s.track(upstream, true)
			upstreamReader = bufio.NewReaderSize(upstream, pipeChunkBytes)
		}
		if !s.exchange(client, clientReader, upstream, upstreamReader, request) {
			return
		}
	}
}

// dialProxy creates one central connection with the transport selected at startup. A TLS error is
// returned to the workload and never retried as bearer authentication.
func (s *sidecar) dialProxy() (net.Conn, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return s.proxyHop.transport.dial(ctx, s.proxyHop.address)
}

// exchange forwards one request and relays its response. It reports whether the connection pair
// may carry another request.
func (s *sidecar) exchange(client net.Conn, clientReader *bufio.Reader, upstream net.Conn, upstreamReader *bufio.Reader, request *http.Request) bool {
	// The body is written concurrently so an interim response (100 Continue) reaches a client that
	// waits for it before sending the body.
	written := make(chan error, 1)
	go func() { written <- request.WriteProxy(upstream) }()
	abort := func() bool {
		_ = client.Close()
		_ = upstream.Close()
		<-written
		return false
	}
	response, err := readFinalResponse(client, upstreamReader, request)
	if err != nil {
		slog.Warn("relaying response from central proxy failed", "error", err)
		return abort()
	}
	if (request.Method == http.MethodConnect && response.StatusCode/100 == 2) || response.StatusCode == http.StatusSwitchingProtocols {
		if err := writeResponseHead(client, response); err != nil {
			return abort()
		}
		if err := <-written; err != nil {
			slog.Warn("writing request to central proxy failed", "error", err)
			return false
		}
		if err := pipeDuplex(client, clientReader, upstream, upstreamReader); err != nil {
			slog.Warn("relay ended with connection error", "upstream", s.proxyHop.address, "error", err)
		}
		return false
	}
	if request.Method == http.MethodConnect {
		// A refused CONNECT ends the connection, as the central proxy ends its side.
		response.Close = true
	}
	if err := response.Write(client); err != nil {
		slog.Warn("relaying response to client failed", "error", err)
		return abort()
	}
	if response.Close || request.Close {
		// Closing upstream first stops the writer from waiting on a body nobody will read, and the
		// client is closed only after its response, so the response is not lost to a reset.
		_ = upstream.Close()
		<-written
		return false
	}
	if err := <-written; err != nil {
		slog.Warn("writing request to central proxy failed", "error", err)
		return false
	}
	return true
}

// readFinalResponse relays interim 1xx responses to the client and returns the first other one;
// 101 Switching Protocols is final.
func readFinalResponse(client io.Writer, upstreamReader *bufio.Reader, request *http.Request) (*http.Response, error) {
	for {
		response, err := http.ReadResponse(upstreamReader, request)
		if err != nil {
			return nil, err
		}
		if response.StatusCode/100 != 1 || response.StatusCode == http.StatusSwitchingProtocols {
			return response, nil
		}
		if err := response.Write(client); err != nil {
			return nil, err
		}
	}
}

// rewriteRequest replaces the workload's proxy credentials with the sidecar's own. http.ReadRequest
// canonicalizes header keys and joins obs-folded lines into the value they continue, so each Del
// removes every value of that header however the workload spelled, repeated or folded it.
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
	// Request.Write sends Go's default User-Agent when the key is absent; an empty value sends none.
	if _, ok := request.Header["User-Agent"]; !ok {
		request.Header["User-Agent"] = []string{""}
	}
}

// writeResponseHead relays the head of a response after which the connection carries tunnel bytes.
// Response.Write would frame a body that a successful CONNECT or a 101 does not have.
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

func writeRefusal(writer io.Writer, status int, reason string) {
	_ = (&http.Response{
		StatusCode: status,
		ProtoMajor: 1,
		ProtoMinor: 1,
		Header:     http.Header{http.CanonicalHeaderKey(refusedHeader): {"reason=" + reason}},
		Close:      true,
	}).Write(writer)
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

type copyResult struct{ err error }

func pipeDuplex(client net.Conn, clientReader *bufio.Reader, upstream net.Conn, upstreamReader *bufio.Reader) error {
	results := make(chan copyResult, 2)
	go func() {
		_, err := io.Copy(upstream, clientReader)
		if tcp, okay := upstream.(*net.TCPConn); okay {
			_ = tcp.CloseWrite()
		}
		results <- copyResult{err: err}
	}()
	go func() {
		_, err := io.Copy(client, upstreamReader)
		if tcp, okay := client.(*net.TCPConn); okay {
			_ = tcp.CloseWrite()
		}
		results <- copyResult{err: err}
	}()
	first := <-results
	if first.err != nil {
		_ = client.Close()
		_ = upstream.Close()
	}
	second := <-results
	if first.err != nil {
		return first.err
	}
	return second.err
}
