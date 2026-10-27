package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/sidecar/configpb"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/util/netlistener"
)

const (
	refusedHeader        = "x-agentplane-egress-sidecar"
	projectedTokenHeader = "X-Agentplane-Workload-Token"
	readinessPath        = "/readyz"
	maxHeadBytes         = 64 * 1024
	pipeChunkBytes       = 64 * 1024
)

type sidecar struct {
	config *configpb.Config
	ready  atomic.Bool

	readinessServer   *http.Server
	readinessListener net.Listener
	proxyListener     net.Listener
	routes            []*platformRoute

	activeMu sync.Mutex
	active   map[net.Conn]struct{}
}

func (s *sidecar) run(ctx context.Context) error {
	readinessListener, err := netlistener.Open(s.config.GetReadinessListenAddress())
	if err != nil {
		return fmt.Errorf("start readiness listener: %w", err)
	}

	routes, err := bindPlatformRoutes(s.config.GetPlatformRoutes())
	if err != nil {
		_ = readinessListener.Close()
		return err
	}

	proxyListener, err := netlistener.Open(s.config.GetListenAddress())
	if err != nil {
		_ = readinessListener.Close()
		stopPlatformRoutes(routes)
		return fmt.Errorf("start HTTP proxy listener: %w", err)
	}
	return s.serve(ctx, proxyListener, readinessListener, routes)
}

func (s *sidecar) serve(ctx context.Context, proxyListener, readinessListener net.Listener, routes []*platformRoute) error {
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
	slog.Info("relaying HTTP proxy traffic", "listen", proxyListener.Addr(), "upstream", net.JoinHostPort(s.config.GetProxyHost(), strconv.Itoa(int(s.config.GetProxyPort()))))

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
	for conn := range s.active {
		_ = conn.Close()
	}
	s.activeMu.Unlock()
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

func (s *sidecar) handle(client net.Conn) {
	defer func() {
		s.track(client, false)
		_ = client.Close()
	}()
	reader := bufio.NewReaderSize(client, pipeChunkBytes)
	head, err := readRequestHead(reader)
	if err != nil {
		slog.Warn("refusing malformed request", "error", err)
		writeRefusal(client, http.StatusBadRequest, "Bad Request", "bad-request")
		return
	}
	connect, err := isConnectRequest(head)
	if err != nil {
		slog.Warn("refusing malformed request", "error", err)
		writeRefusal(client, http.StatusBadRequest, "Bad Request", "bad-request")
		return
	}
	token, err := readToken(s.config.GetTokenFile())
	if err != nil {
		slog.Error("central proxy token unavailable", "path", s.config.GetTokenFile(), "error", err)
		writeRefusal(client, http.StatusServiceUnavailable, "Service Unavailable", "token-unavailable")
		return
	}
	projected := s.readProjectedTokens()
	forwarded := rewriteRequestHead(head, token, projected, connect)
	upstream, err := (&net.Dialer{Timeout: 10 * time.Second}).Dial("tcp", net.JoinHostPort(s.config.GetProxyHost(), strconv.Itoa(int(s.config.GetProxyPort()))))
	if err != nil {
		slog.Warn("central proxy unreachable", "host", s.config.GetProxyHost(), "port", int(s.config.GetProxyPort()), "error", err)
		writeRefusal(client, http.StatusBadGateway, "Bad Gateway", "proxy-unreachable")
		return
	}
	s.track(upstream, true)
	defer func() {
		s.track(upstream, false)
		_ = upstream.Close()
	}()
	if err := writeBytes(upstream, forwarded); err != nil {
		slog.Warn("writing request to central proxy failed", "error", err)
		return
	}
	upstreamReader := bufio.NewReaderSize(upstream, pipeChunkBytes)
	if connect {
		responseHead, err := readResponseHead(upstreamReader)
		if err != nil {
			slog.Warn("reading CONNECT response from central proxy failed", "error", err)
			return
		}
		if err := writeBytes(client, responseHead); err != nil {
			return
		}
		statusCode, err := responseStatus(responseHead)
		if err != nil {
			slog.Warn("invalid CONNECT response from central proxy", "error", err)
			return
		}
		if statusCode < 200 || statusCode >= 300 {
			if _, err := io.Copy(client, upstreamReader); err != nil {
				slog.Warn("relaying CONNECT refusal body failed", "error", err)
			}
			return
		}
	}
	if err := pipeDuplex(client, reader, upstream, upstreamReader); err != nil {
		slog.Warn("relay ended with connection error", "host", s.config.GetProxyHost(), "port", int(s.config.GetProxyPort()), "error", err)
	}
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
	tokens := make(map[string]string, len(s.config.GetAudienceTokenFiles()))
	for audience, path := range s.config.GetAudienceTokenFiles() {
		if token, err := readToken(path); err == nil {
			tokens[audience] = token
		} else {
			slog.Warn("projected audience token unavailable", "audience", audience, "path", path, "error", err)
		}
	}
	return tokens
}

func readRequestHead(reader *bufio.Reader) ([]byte, error) { return readHead(reader, "request") }

func readResponseHead(reader *bufio.Reader) ([]byte, error) { return readHead(reader, "response") }

func readHead(reader *bufio.Reader, kind string) ([]byte, error) {
	var head bytes.Buffer
	for {
		line, err := reader.ReadSlice('\n')
		if err != nil {
			return nil, err
		}
		if !bytes.HasSuffix(line, []byte("\r\n")) {
			return nil, fmt.Errorf("%s head has a non-HTTP line ending", kind)
		}
		if _, err := head.Write(line); err != nil {
			return nil, err
		}
		if head.Len() > maxHeadBytes {
			return nil, fmt.Errorf("%s head exceeds %d bytes", kind, maxHeadBytes)
		}
		if bytes.Equal(line, []byte("\r\n")) {
			return head.Bytes(), nil
		}
	}
}

func isConnectRequest(head []byte) (bool, error) {
	line, _, found := bytes.Cut(head, []byte("\r\n"))
	if !found {
		return false, errors.New("request line is not terminated")
	}
	parts := strings.Split(string(line), " ")
	if len(parts) != 3 || !strings.HasPrefix(parts[2], "HTTP/1.") {
		return false, fmt.Errorf("not an HTTP/1.x request line: %q", line)
	}
	return parts[0] == "CONNECT", nil
}

func rewriteRequestHead(head []byte, token string, projected map[string]string, connect bool) []byte {
	lines := bytes.Split(head[:len(head)-4], []byte("\r\n"))
	rewritten := make([][]byte, 0, len(lines)+len(projected)+3)
	rewritten = append(rewritten, lines[0])
	for _, line := range lines[1:] {
		name, _, _ := bytes.Cut(line, []byte(":"))
		name = bytes.ToLower(bytes.TrimSpace(name))
		nameString := string(name)
		if nameString == "proxy-authorization" || nameString == "proxy-connection" ||
			nameString == strings.ToLower(projectedTokenHeader) {
			continue
		}
		if !connect && nameString == "connection" {
			continue
		}
		rewritten = append(rewritten, line)
	}
	if !connect {
		rewritten = append(rewritten, []byte("Connection: close"))
	}
	rewritten = append(rewritten, []byte("Proxy-Authorization: Bearer "+token))
	audiences := make([]string, 0, len(projected))
	for audience := range projected {
		audiences = append(audiences, audience)
	}
	sort.Strings(audiences)
	for _, audience := range audiences {
		rewritten = append(rewritten, []byte(projectedTokenHeader+": "+audience+" "+projected[audience]))
	}
	return append(bytes.Join(rewritten, []byte("\r\n")), '\r', '\n', '\r', '\n')
}

func responseStatus(head []byte) (int, error) {
	line, _, found := bytes.Cut(head, []byte("\r\n"))
	parts := strings.SplitN(string(line), " ", 3)
	if !found || len(parts) < 2 || !strings.HasPrefix(parts[0], "HTTP/1.") {
		return 0, fmt.Errorf("not an HTTP/1.x status line: %q", line)
	}
	status, err := strconv.Atoi(parts[1])
	if err != nil {
		return 0, fmt.Errorf("invalid HTTP status: %w", err)
	}
	return status, nil
}

func writeRefusal(writer io.Writer, status int, phrase, reason string) {
	response := fmt.Sprintf("HTTP/1.1 %d %s\r\n%s: reason=%s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
		status, phrase, refusedHeader, reason)
	_ = writeBytes(writer, []byte(response))
}

func writeBytes(writer io.Writer, data []byte) error {
	for len(data) > 0 {
		n, err := writer.Write(data)
		if err != nil {
			return err
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		data = data[n:]
	}
	return nil
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
