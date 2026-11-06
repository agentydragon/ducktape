package main

import (
	"bufio"
	"context"
	"crypto/tls"
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
		route := &workloadRoute{config: config}
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
	token := ""
	if s.config.ProxyMode == "token" {
		token, err = readToken(s.config.TokenFile)
		if err != nil {
			return nil, err
		}
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
	inner := tls.Client(tracked, security.Clone())
	if err := inner.HandshakeContext(ctx); err != nil {
		return nil, fmt.Errorf("workload service TLS: %w", err)
	}
	if err := connection.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	s.track(tracked, true)
	complete = true
	return inner, nil
}

type bufferedWorkloadConnection struct {
	net.Conn
	reader *bufio.Reader
}

func (c *bufferedWorkloadConnection) Read(p []byte) (int, error) { return c.reader.Read(p) }

type trackedWorkloadConnection struct {
	net.Conn
	sidecar *sidecar
}

func (c *trackedWorkloadConnection) Close() error {
	c.sidecar.track(c, false)
	return c.Conn.Close()
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
	if err := response.Write(client); err != nil {
		return false
	}
	return !request.Close && !response.Close
}
