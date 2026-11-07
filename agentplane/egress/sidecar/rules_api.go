package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
)

// The Rules API uses the forward hop's authentication and server identity.
// The workload sends its existing HTTP URL; the credential-holding sidecar
// establishes TLS through the proxy without asserting a caller to the API.
func (s *sidecar) prepareRulesAPI() error {
	if s.config.RulesAPI == "" {
		return nil
	}
	hop, ok := s.proxyHop.transport.(tlsProxyTransport)
	if !ok {
		return errors.New("rules_api requires TLS proxy transport")
	}
	s.rulesTransport = &http.Transport{
		ForceAttemptHTTP2: true,
		DialTLSContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			if network != "tcp" || address != s.config.RulesAPI {
				return nil, errors.New("unexpected Rules API destination")
			}
			return s.dialTLSRoute(ctx, address, hop.security)
		},
		IdleConnTimeout: time.Minute, ExpectContinueTimeout: time.Second,
	}
	s.rulesHandler = &httputil.ReverseProxy{
		Transport: s.rulesTransport,
		Rewrite: func(r *httputil.ProxyRequest) {
			r.Out.URL.Scheme = "https"
			r.Out.URL.Host = s.config.RulesAPI
			r.Out.Host = s.config.RulesAPI
			r.Out.Header.Del("Authorization")
			r.Out.Header.Del("Proxy-Authorization")
			r.Out.Header.Del(projectedTokenHeader)
			if token := r.In.Context().Value(proxyTokenKey{}).(string); token != "" {
				r.Out.Header.Set("Authorization", "Bearer "+token)
			}
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			slog.Warn("Rules API TLS failed", "error", err)
			refuse(w, http.StatusBadGateway, "rules-api-unavailable")
		},
	}
	return nil
}

// dialTLSRoute is the CONNECT glue for TLS whose local and peer leaf expiry
// bound the connection lifetime. HTTP request parsing, framing and forwarding
// remain with net/http and ReverseProxy.
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
			connection.Close()
		}
	}()
	deadline, _ := ctx.Deadline()
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
		return nil, fmt.Errorf("central proxy refused CONNECT: %s", response.Status)
	}
	tracked := &trackedClientConn{Conn: &bufferedTLSRoute{Conn: connection, reader: reader}, sidecar: s}
	inner, err := environment_runtime_transport.ClientTLSConnection(ctx, tracked, security)
	if err != nil {
		return nil, fmt.Errorf("destination TLS: %w", err)
	}
	if err := connection.SetDeadline(time.Time{}); err != nil {
		inner.Close()
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
