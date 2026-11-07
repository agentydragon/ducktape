package gateway

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/v1/providerpb"
)

// providerDocument retrieves provider-owned documentation without sending
// browser or consumer credentials to the provider. It never follows redirects.
func providerDocument(ctx context.Context, provider Provider, httpConfig ProviderHTTPConfig) (map[string]any, error) {
	info, err := provider.Client.GetInfo(ctx, &providerpb.GetInfoRequest{})
	if err != nil || info == nil || !providerInfoValid(info.GetProvider()) || info.GetProvider().GetProviderId() != provider.Ref.ProviderId {
		return nil, errors.New("provider information unavailable")
	}
	var path string
	for _, item := range info.GetProvider().GetInstances() {
		if item.GetInstanceId() == provider.Ref.InstanceId && item.OpenapiDocumentPath != nil {
			path = item.GetOpenapiDocumentPath()
			break
		}
	}
	if path == "" || strings.Contains(path, "*") {
		return nil, errors.New("provider document unavailable")
	}
	if _, err := segments(path, false); err != nil {
		return nil, errors.New("invalid provider document path")
	}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimSuffix(httpConfig.Origin, "/")+path, nil)
	if err != nil {
		return nil, errors.New("invalid provider document request")
	}
	request.Header.Set("Authorization", "Bearer "+httpConfig.OwnerToken)
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	if httpConfig.CAFile != "" {
		pem, err := os.ReadFile(httpConfig.CAFile)
		pool := x509.NewCertPool()
		if err != nil || !pool.AppendCertsFromPEM(pem) {
			return nil, errors.New("invalid provider CA")
		}
		tlsConfig.RootCAs = pool
	}
	transport := &http.Transport{TLSClientConfig: tlsConfig, DisableKeepAlives: true, DisableCompression: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(request)
	if err != nil {
		return nil, errors.New("provider document unavailable")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, errors.New("provider document unavailable")
	}
	content, err := io.ReadAll(io.LimitReader(response.Body, 8*1024*1024+1))
	if err != nil || len(content) > 8*1024*1024 {
		return nil, errors.New("invalid provider document")
	}
	var document map[string]any
	if json.Unmarshal(content, &document) != nil || document == nil {
		return nil, errors.New("invalid provider document")
	}
	document["servers"] = []map[string]string{{"url": "/proxy/" + provider.Ref.InstanceId}}
	return document, nil
}
