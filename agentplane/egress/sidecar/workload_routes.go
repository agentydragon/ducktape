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
	"net/http/httputil"
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
	handler   *httputil.ReverseProxy
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
			ForceAttemptHTTP2:     true,
			ExpectContinueTimeout: time.Second, IdleConnTimeout: time.Minute,
			DialTLSContext: func(ctx context.Context, network, address string) (net.Conn, error) {
				if network != "tcp" || address != config.Upstream {
					return nil, errors.New("unexpected workload route destination")
				}
				return s.dialTLSRoute(ctx, address, security)
			},
		}
		route.handler = &httputil.ReverseProxy{
			Transport: route.transport,
			Rewrite: func(r *httputil.ProxyRequest) {
				r.Out.RequestURI = ""
				r.Out.URL.Scheme = "https"
				r.Out.URL.Host = config.Upstream
				r.Out.Host = config.Upstream
				r.Out.Header.Del("Authorization")
				r.Out.Header.Del("Proxy-Authorization")
				r.Out.Header.Del("Proxy-Connection")
				r.Out.Header.Del(projectedTokenHeader)
			},
			ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
				slog.Warn("workload TLS route refused", "upstream", config.Upstream, "error", err)
				refuse(w, http.StatusBadGateway, "workload-tls-unavailable")
			},
		}
		s.workloadRoutes[authority] = route
	}
	return nil
}

// dialTLSRoute verifies the service TLS peer inside the authenticated central CONNECT tunnel.
func (s *sidecar) dialTLSRoute(ctx context.Context, target string, security *tls.Config) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	connection, err := s.dialRouteTunnel(ctx, target)
	if err != nil {
		return nil, err
	}
	secured, err := environment_runtime_transport.ClientTLSConnection(ctx, connection, security)
	if err != nil {
		_ = connection.Close()
		return nil, fmt.Errorf("destination TLS: %w", err)
	}
	return secured, nil
}

func validateWorkloadRoute(authority string, config workloadRouteConfig) error {
	for _, address := range []string{authority, config.Upstream} {
		if err := validateAuthority(address); err != nil {
			return fmt.Errorf("workload route address: %w", err)
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

// exchangeWorkloadConnectRoute carries native h2c gRPC frames through the caller's existing HTTP
// CONNECT authority. The sidecar adds destination TLS and its Runner SVID after central admission;
// the central proxy sees only opaque service TLS bytes.
func (s *sidecar) exchangeWorkloadConnectRoute(w http.ResponseWriter, request *http.Request, route *workloadRoute) {
	ctx, cancel := context.WithTimeout(s.workloadContext, 10*time.Second)
	defer cancel()
	upstream, err := s.dialTLSRoute(ctx, route.config.Upstream, route.security)
	if err != nil {
		slog.Warn("workload CONNECT route refused", "upstream", route.config.Upstream, "error", err)
		refuse(w, http.StatusBadGateway, "workload-tls-unavailable")
		return
	}
	defer upstream.Close()
	client, buffered, err := http.NewResponseController(w).Hijack()
	if err != nil {
		return
	}
	defer client.Close()
	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		return
	}
	if err := pipeWorkloadTunnel(client, buffered.Reader, upstream); err != nil {
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

func (s *sidecar) exchangeWorkloadRoute(w http.ResponseWriter, request *http.Request, route *workloadRoute) {
	route.handler.ServeHTTP(w, request)
}
