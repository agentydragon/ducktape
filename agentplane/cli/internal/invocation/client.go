package invocation

import (
	"context"
	"crypto/tls"
	"net"
	"net/url"
	"strconv"
	"strings"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

// parseServiceURL validates a configured service endpoint without including its credential in errors.
func parseServiceURL(base, variable string) (*url.URL, error) {
	parsed, err := url.Parse(base)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" ||
		parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, UsageFailure("%s must be an http(s) base URL without credentials, query or fragment", variable)
	}
	return parsed, nil
}

// grpcTarget preserves the configured HTTP(S) origin's default port. grpc.NewClient's passthrough
// resolver otherwise treats a bare host as port 443 even when the URL says http.
func grpcTarget(parsed *url.URL, variable string) (string, error) {
	if strings.HasSuffix(parsed.Host, ":") {
		return "", UsageFailure("%s must not have an empty port", variable)
	}
	host := parsed.Hostname()
	if host == "" {
		return "", UsageFailure("%s must include a host", variable)
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
			return "", UsageFailure("%s has invalid port %q", variable, port)
		}
		port = strconv.Itoa(portNumber)
	}
	return net.JoinHostPort(host, port), nil
}

type bearerCredential string

func (c bearerCredential) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	return map[string]string{"authorization": "Bearer " + string(c)}, nil
}

func (bearerCredential) RequireTransportSecurity() bool { return false }

// Managed endpoints go through the credential-holding sidecar. With HTTPS_PROXY configured,
// grpc-go can use CONNECT even for an HTTP origin.
func (a *Invocation) connectGRPC(group string) (*grpc.ClientConn, error) {
	urlVariable, credentialVariable := "AGENTPLANE_"+group+"_URL", "AGENTPLANE_"+group+"_CREDENTIAL"
	base, credential := a.getenv(urlVariable), a.getenv(credentialVariable)
	if base == "" {
		return nil, UsageFailure("%s not set", urlVariable)
	}
	parsed, err := parseServiceURL(base, urlVariable)
	if err != nil {
		return nil, err
	}
	if parsed.Path != "" && parsed.Path != "/" {
		return nil, UsageFailure("%s must not include a path", urlVariable)
	}
	target, err := grpcTarget(parsed, urlVariable)
	if err != nil {
		return nil, err
	}
	transportCredentials := credentials.TransportCredentials(insecure.NewCredentials())
	if parsed.Scheme == "https" {
		transportCredentials = credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS12, ServerName: parsed.Hostname()})
	}
	options := []grpc.DialOption{
		grpc.WithTransportCredentials(transportCredentials),
		grpc.WithDisableRetry(),
	}
	if credential != "" {
		options = append(options, grpc.WithPerRPCCredentials(bearerCredential(credential)))
	}
	connection, err := grpc.NewClient(target, options...)
	if err != nil {
		return nil, UsageFailure("%s is not a valid gRPC endpoint", urlVariable)
	}
	return connection, nil
}
