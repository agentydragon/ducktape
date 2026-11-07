package main

import (
	"context"
	"crypto/tls"
	"net"
	"net/url"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/spf13/cobra"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

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
