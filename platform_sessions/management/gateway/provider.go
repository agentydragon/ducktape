package gateway

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
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
func ConnectProvider(ref *providerpb.InstanceRef, target, tokenPath, caPath string, timeout time.Duration) (*ProviderConnection, error) {
	if ref == nil || ref.ProviderId == "" || ref.InstanceId == "" || target == "" || tokenPath == "" || timeout <= 0 {
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
	interceptor := func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoke grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		bounded, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		return invoke(bounded, method, req, reply, cc, opts...)
	}
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12})), grpc.WithPerRPCCredentials(tokenFile{tokenPath}), grpc.WithDisableRetry(), grpc.WithUnaryInterceptor(interceptor))
	if err != nil {
		return nil, errors.New("could not initialize provider connection")
	}
	return &ProviderConnection{Provider: Provider{Ref: ref, Client: providerpb.NewSessionProviderControlClient(conn)}, conn: conn}, nil
}
func (c *ProviderConnection) Close() error { return c.conn.Close() }
