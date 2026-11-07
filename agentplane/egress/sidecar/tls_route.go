package main

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"time"
)

// dialRouteTunnel opens an authenticated CONNECT tunnel through the central proxy.
func (s *sidecar) dialRouteTunnel(ctx context.Context, target string) (net.Conn, error) {
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
	if err := connection.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	tracked := &trackedClientConn{Conn: &bufferedRouteConn{Conn: connection, reader: reader}, sidecar: s}
	s.track(tracked, true)
	complete = true
	return tracked, nil
}

type bufferedRouteConn struct {
	net.Conn
	reader *bufio.Reader
}

func (c *bufferedRouteConn) Read(p []byte) (int, error) { return c.reader.Read(p) }
