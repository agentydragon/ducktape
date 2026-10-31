package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/spf13/cobra"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// httpClient uses the default transport, so HTTPS_PROXY, HTTP_PROXY, NO_PROXY and SSL_CERT_FILE
// behave as they do for curl. It follows no redirect and has no way to skip verification.
var httpClient = &http.Client{
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

// service is one configured service: the base URL and the placeholder credential it is called with.
type service struct {
	baseURL    string
	credential string
}

// request is one call. A 409 carries the gRPC code in conflict.
type request struct {
	method   string
	path     string
	query    url.Values
	body     any
	conflict string
}

// connect reads the group's configuration from the environment. Only that group's variables are
// consulted, so a group is usable whatever the others lack.
func (a *app) connect(group string) (*service, error) {
	urlVariable, credentialVariable := "AGENTPLANE_"+group+"_URL", "AGENTPLANE_"+group+"_CREDENTIAL"
	base, credential := a.getenv(urlVariable), a.getenv(credentialVariable)
	var missing []string
	if base == "" {
		missing = append(missing, urlVariable)
	}
	if credential == "" {
		missing = append(missing, credentialVariable)
	}
	if len(missing) > 0 {
		return nil, usageFailure("%s not set", strings.Join(missing, " and "))
	}
	if _, err := parseServiceURL(base, urlVariable); err != nil {
		return nil, err
	}
	return &service{baseURL: strings.TrimRight(base, "/"), credential: credential}, nil
}

// parseServiceURL validates a configured service endpoint without including its credential in errors.
func parseServiceURL(base, variable string) (*url.URL, error) {
	parsed, err := url.Parse(base)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" ||
		parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, usageFailure("%s must be an http(s) base URL without credentials, query or fragment", variable)
	}
	return parsed, nil
}

// grpcTarget preserves the configured HTTP(S) origin's default port. grpc.NewClient's passthrough
// resolver otherwise treats a bare host as port 443 even when the URL says http.
func grpcTarget(parsed *url.URL, variable string) (string, error) {
	if strings.HasSuffix(parsed.Host, ":") {
		return "", usageFailure("%s must not have an empty port", variable)
	}
	host := parsed.Hostname()
	if host == "" {
		return "", usageFailure("%s must include a host", variable)
	}
	port := parsed.Port()
	if port == "" {
		if parsed.Scheme == "http" {
			port = "80"
		} else {
			port = "443"
		}
	} else {
		portNumber, err := strconv.Atoi(port)
		if err != nil || portNumber < 1 || portNumber > 65535 {
			return "", usageFailure("%s has invalid port %q", variable, port)
		}
		port = strconv.Itoa(portNumber)
	}
	return net.JoinHostPort(host, port), nil
}

// actionService is a generated gRPC client of the Action Service. The configured credential is sent
// as Authorization metadata on each call so the egress proxy can substitute it. grpc-go resolves its
// proxy from HTTPS_PROXY even for an http origin and uses CONNECT, carrying h2c inside the tunnel.
type actionService struct {
	connection *grpc.ClientConn
	client     action_servicepb.ActionServiceClient
}

type bearerCredential string

func (c bearerCredential) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	return map[string]string{"authorization": "Bearer " + string(c)}, nil
}

func (bearerCredential) RequireTransportSecurity() bool { return false }

func (a *app) connectGRPC(group string) (*grpc.ClientConn, error) {
	urlVariable, credentialVariable := "AGENTPLANE_"+group+"_URL", "AGENTPLANE_"+group+"_CREDENTIAL"
	base, credential := a.getenv(urlVariable), a.getenv(credentialVariable)
	var missing []string
	if base == "" {
		missing = append(missing, urlVariable)
	}
	if credential == "" {
		missing = append(missing, credentialVariable)
	}
	if len(missing) > 0 {
		return nil, usageFailure("%s not set", strings.Join(missing, " and "))
	}
	parsed, err := parseServiceURL(base, urlVariable)
	if err != nil {
		return nil, err
	}
	if parsed.Path != "" && parsed.Path != "/" {
		return nil, usageFailure("%s must not include a path", urlVariable)
	}
	target, err := grpcTarget(parsed, urlVariable)
	if err != nil {
		return nil, err
	}
	transportCredentials := credentials.TransportCredentials(insecure.NewCredentials())
	if parsed.Scheme == "https" {
		transportCredentials = credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS12, ServerName: parsed.Hostname()})
	}
	connection, err := grpc.NewClient(
		target,
		grpc.WithTransportCredentials(transportCredentials),
		grpc.WithPerRPCCredentials(bearerCredential(credential)),
		grpc.WithDisableRetry(),
	)
	if err != nil {
		return nil, usageFailure("%s is not a valid gRPC endpoint", urlVariable)
	}
	return connection, nil
}

func (a *app) connectActions() (*actionService, error) {
	connection, err := a.connectGRPC("ACTIONS")
	if err != nil {
		return nil, err
	}
	return &actionService{connection: connection, client: action_servicepb.NewActionServiceClient(connection)}, nil
}

type actionCall func(context.Context, action_servicepb.ActionServiceClient, []string) error

func (a *app) actionCommand(fn actionCall) func(*cobra.Command, []string) error {
	return func(_ *cobra.Command, args []string) error {
		connected, err := a.connectActions()
		if err != nil {
			return err
		}
		defer connected.connection.Close()
		ctx, cancel := a.deadline()
		defer cancel()
		return fn(ctx, connected.client, args)
	}
}

// do makes one call and returns the body of its 2xx answer. Any other outcome is a *failure, or
// errInterrupted when a signal ended the call.
func (s *service) do(ctx context.Context, r request) ([]byte, error) {
	var body io.Reader
	if r.body != nil {
		var encoded []byte
		var err error
		if message, ok := r.body.(proto.Message); ok {
			encoded, err = protojson.Marshal(message)
		} else {
			encoded, err = json.Marshal(r.body)
		}
		if err != nil {
			return nil, clientFailure(exitUsage, "INVALID_ARGUMENT", "request body: %v", err)
		}
		body = bytes.NewReader(encoded)
	}
	target := s.baseURL + r.path
	if len(r.query) > 0 {
		target += "?" + r.query.Encode()
	}
	var sent atomic.Bool
	trace := &httptrace.ClientTrace{WroteRequest: func(info httptrace.WroteRequestInfo) {
		if info.Err == nil {
			sent.Store(true)
		}
	}}
	req, err := http.NewRequestWithContext(httptrace.WithClientTrace(ctx, trace), r.method, target, body)
	if err != nil {
		return nil, clientFailure(exitUsage, "INVALID_ARGUMENT", "request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+s.credential)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	response, err := httpClient.Do(req)
	if err != nil {
		return nil, transportFailure(ctx, err, sent.Load())
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, transportFailure(ctx, err, true)
	}
	if response.StatusCode < 200 || response.StatusCode > 299 {
		conflict := r.conflict
		if conflict == "" {
			conflict = "ABORTED"
		}
		return nil, httpFailure(response.StatusCode, response.Header, payload, conflict)
	}
	return payload, nil
}

// fetch keeps the JSON GET path shared by Notifications and Egress clients.
func (s *service) fetch(ctx context.Context, path string, query url.Values) ([]byte, error) {
	return s.do(ctx, request{method: http.MethodGet, path: path, query: query})
}

func transportFailure(ctx context.Context, err error, sent bool) error {
	if errors.Is(ctx.Err(), context.Canceled) {
		return errInterrupted
	}
	code := "UNAVAILABLE"
	var network net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &network) && network.Timeout()) {
		code = "DEADLINE_EXCEEDED"
	}
	result := clientFailure(exitTransient, code, "%v", err)
	result.sent = sent
	return result
}
