package gateway

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"math"
	"os"
	"strings"
	"time"

	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/v1/providerpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

// ProviderConnection owns a TLS channel to one independently running provider.
type ProviderConnection struct {
	Provider Provider
	conn     *grpc.ClientConn
}

// ProviderTimeouts separates fast status reads from operations that may wait for
// provider-owned browser or upstream work.
type ProviderTimeouts struct {
	Status    time.Duration
	Operation time.Duration
}

const maxProviderTimeout = 10 * time.Minute

// ProviderTimeoutsFromSeconds validates the numeric values used by the gateway
// deployment configuration.
func ProviderTimeoutsFromSeconds(statusSeconds, operationSeconds float64) (ProviderTimeouts, error) {
	valid := func(seconds float64) bool {
		return !math.IsNaN(seconds) && !math.IsInf(seconds, 0) && seconds > 0 && seconds <= maxProviderTimeout.Seconds()
	}
	if !valid(statusSeconds) || !valid(operationSeconds) || operationSeconds < statusSeconds {
		return ProviderTimeouts{}, errors.New("invalid provider deadlines")
	}
	timeouts := ProviderTimeouts{
		Status:    time.Duration(statusSeconds * float64(time.Second)),
		Operation: time.Duration(operationSeconds * float64(time.Second)),
	}
	if timeouts.Status <= 0 || timeouts.Operation < timeouts.Status {
		return ProviderTimeouts{}, errors.New("invalid provider deadlines")
	}
	return timeouts, nil
}

type tokenFile struct{ path string }

func (t tokenFile) RequireTransportSecurity() bool { return true }
func (t tokenFile) GetRequestMetadata(_ context.Context, _ ...string) (map[string]string, error) {
	data, err := os.ReadFile(t.path)
	if err != nil {
		return nil, errors.New("provider credential unavailable")
	}
	token := strings.TrimSpace(string(data))
	if len(token) < 32 {
		return nil, errors.New("invalid provider credential")
	}
	for _, c := range token {
		if c < 33 || c > 126 {
			return nil, errors.New("invalid provider credential")
		}
	}
	return map[string]string{"authorization": "Bearer " + token}, nil
}

// ConnectProvider disables retries because a lost import or probe can have
// changed upstream credentials. The token file is re-read for each RPC.
func ConnectProvider(ref *providerpb.InstanceRef, target, tokenPath, caPath string, timeouts ProviderTimeouts) (*ProviderConnection, error) {
	if ref == nil || ref.ProviderId == "" || ref.InstanceId == "" || target == "" || tokenPath == "" ||
		timeouts.Status <= 0 || timeouts.Operation < timeouts.Status || timeouts.Operation > maxProviderTimeout {
		return nil, errors.New("invalid provider connection")
	}
	roots, err := x509.SystemCertPool()
	if err != nil {
		return nil, errors.New("could not load system certificate pool")
	}
	if caPath != "" {
		roots = x509.NewCertPool()
		pem, err := os.ReadFile(caPath)
		if err != nil || !roots.AppendCertsFromPEM(pem) {
			return nil, errors.New("invalid provider CA")
		}
	}
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12})), grpc.WithPerRPCCredentials(tokenFile{tokenPath}), grpc.WithDisableRetry(), grpc.WithUnaryInterceptor(providerDeadlineInterceptor(timeouts)))
	if err != nil {
		return nil, errors.New("could not initialize provider connection")
	}
	return &ProviderConnection{Provider: Provider{Ref: ref, Client: providerpb.NewSessionProviderControlClient(conn)}, conn: conn}, nil
}
func (c *ProviderConnection) Close() error { return c.conn.Close() }

func providerCallTimeout(method string, timeouts ProviderTimeouts) time.Duration {
	switch method {
	case providerpb.SessionProviderControl_GetInfo_FullMethodName,
		providerpb.SessionProviderControl_GetSession_FullMethodName:
		return timeouts.Status
	default:
		return timeouts.Operation
	}
}

func providerDeadlineInterceptor(timeouts ProviderTimeouts) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoke grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		bounded, cancel := context.WithTimeout(ctx, providerCallTimeout(method, timeouts))
		defer cancel()
		return invoke(bounded, method, req, reply, cc, opts...)
	}
}
