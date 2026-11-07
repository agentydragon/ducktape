package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
)

// dialTLSRoute opens a destination TLS connection through the authenticated
// central proxy hop. The Environment Runtime SVID source is reloadable and is
// deliberately pinned to the same EgressProxyURI identity as the proxy hop.
func (s *sidecar) dialTLSRoute(ctx context.Context, target string, security *tls.Config) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	connection, err := s.proxyHop.transport.dial(ctx, s.proxyHop.address)
	if err != nil {
		return nil, err
	}
	complete := false
	defer func() {
		if !complete {
			_ = connection.Close()
		}
	}()
	deadline, _ := ctx.Deadline()
	if err := connection.SetDeadline(deadline); err != nil {
		return nil, err
	}
	stop := context.AfterFunc(ctx, func() { _ = connection.Close() })
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
		_ = response.Body.Close()
		return nil, fmt.Errorf("central proxy refused CONNECT: %s", response.Status)
	}
	tracked := &trackedClientConn{Conn: &bufferedTLSRoute{Conn: connection, reader: reader}, sidecar: s}
	inner, err := environment_runtime_transport.ClientTLSConnection(ctx, tracked, security)
	if err != nil {
		return nil, fmt.Errorf("destination TLS: %w", err)
	}
	if err := connection.SetDeadline(time.Time{}); err != nil {
		_ = inner.Close()
		return nil, err
	}
	s.track(tracked, true)
	complete = true
	return inner, nil
}

type bufferedTLSRoute struct {
	net.Conn
	reader *bufio.Reader
}

func (c *bufferedTLSRoute) Read(p []byte) (int, error) { return c.reader.Read(p) }
