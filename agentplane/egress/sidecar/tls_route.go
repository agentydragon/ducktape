package main

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/httpconnect"
)

// dialRouteTunnel opens an authenticated CONNECT tunnel through the central proxy.
func (s *sidecar) dialRouteTunnel(ctx context.Context, target string) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	request := &http.Request{Method: http.MethodConnect, URL: &url.URL{Host: target}, Host: target, Header: make(http.Header)}
	token, err := s.proxyHop.auth.requestToken()
	if err != nil {
		return nil, fmt.Errorf("load proxy hop credential: %w", err)
	}
	rewriteRequest(request, token, nil)
	connection, err := s.proxyHop.transport.dial(ctx, s.proxyHop.address)
	if err != nil {
		return nil, err
	}
	tunnel, err := httpconnect.Open(ctx, connection, target, request.Header)
	if err != nil {
		return nil, fmt.Errorf("central proxy CONNECT: %w", err)
	}
	return tunnel, nil
}
