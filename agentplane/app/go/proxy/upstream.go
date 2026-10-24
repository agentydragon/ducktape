// Package proxy contains the app's HTTP proxy primitives and thread Electric routes.
package proxy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"time"
	"unicode/utf8"
)

// ErrUpstreamRedirect marks a redirect response rejected by a mounted facade proxy.
var ErrUpstreamRedirect = errors.New("upstream redirect is not accepted")

// UpstreamProxy opens streamed HTTP requests to one configured service.
//
// The returned client never follows redirects and has no whole-request timeout. Its default
// transport bounds connection establishment to five seconds, while the caller's context controls
// the lifetime of the response stream.
type UpstreamProxy struct {
	client    *http.Client
	baseURL   *url.URL
	tokenFile string
}

// NewUpstreamProxy creates a proxy for baseURL. tokenFile may be empty for request-bound bearer
// tokens; when set, mounted requests reread it before every request.
func NewUpstreamProxy(client *http.Client, baseURL, tokenFile string) (*UpstreamProxy, error) {
	base, err := url.Parse(baseURL)
	if err != nil {
		return nil, fmt.Errorf("parse upstream URL: %w", err)
	}
	if !base.IsAbs() || base.Host == "" || (base.Scheme != "http" && base.Scheme != "https") {
		return nil, fmt.Errorf("upstream URL must be an absolute HTTP(S) URL")
	}
	base.Path = ""
	base.RawPath = ""
	base.RawQuery = ""
	base.Fragment = ""
	return &UpstreamProxy{client: proxyClient(client), baseURL: base, tokenFile: tokenFile}, nil
}

// OpenRequest opens a streamed request. Only Accept, Content-Type, and User-Agent are forwarded
// from headers; browser credentials and cookies never cross the proxy boundary. The response body
// remains unread and must be closed by the caller.
func (p *UpstreamProxy) OpenRequest(
	ctx context.Context,
	method, path, query, bearer string,
	headers http.Header,
	body io.Reader,
) (*http.Response, error) {
	return p.openRequest(ctx, method, path, query, bearer, headers, body, representationHeaders)
}

// ForwardHTTP streams a mounted HTTP route through net/http's reverse proxy. It preserves the
// request method, body, query, and cancellation while forwarding only the app-approved headers.
// A configured token file is reread for each request and takes precedence over bearer.
func (p *UpstreamProxy) ForwardHTTP(
	w http.ResponseWriter,
	request *http.Request,
	ctx context.Context,
	path string,
	bearer string,
	rejectRedirects bool,
	onError func(error),
) error {
	if p.tokenFile != "" {
		token, err := os.ReadFile(p.tokenFile)
		if err != nil {
			return fmt.Errorf("read projected upstream token: %w", err)
		}
		bearer = strings.TrimSpace(string(token))
	}

	handler := &httputil.ReverseProxy{
		Transport:     p.client.Transport,
		FlushInterval: -1,
		Rewrite: func(proxyRequest *httputil.ProxyRequest) {
			proxyRequest.SetURL(p.baseURL)
			proxyRequest.Out.URL.Path = "/" + strings.TrimLeft(path, "/")
			proxyRequest.Out.URL.RawPath = ""
			proxyRequest.Out.URL.RawQuery = proxyRequest.In.URL.RawQuery
			proxyRequest.Out.Header = make(http.Header)
			proxyRequest.Out.Trailer = nil
			for _, name := range []string{"Accept", "Content-Type", "User-Agent"} {
				value := proxyRequest.In.Header.Get(name)
				if name == "User-Agent" {
					value = truncateRunes(value, 300)
				}
				if value != "" {
					proxyRequest.Out.Header.Set(name, value)
				}
			}
			if bearer != "" {
				proxyRequest.Out.Header.Set("Authorization", "Bearer "+bearer)
			}
			proxyRequest.Out.Header.Set("Accept-Encoding", "identity")
		},
		ModifyResponse: func(response *http.Response) error {
			if rejectRedirects && response.StatusCode >= 300 && response.StatusCode < 400 {
				return ErrUpstreamRedirect
			}
			contentType := response.Header.Values("Content-Type")
			response.Header = make(http.Header)
			if len(contentType) > 0 {
				response.Header.Set("Content-Type", contentType[0])
			}
			response.Header.Set("Cache-Control", "no-store")
			response.Header.Set("X-Accel-Buffering", "no")
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			if onError != nil {
				onError(err)
				return
			}
			http.Error(w, http.StatusText(http.StatusBadGateway), http.StatusBadGateway)
		},
	}
	handler.ServeHTTP(w, request.WithContext(ctx))
	return nil
}

var representationHeaders = map[string]struct{}{
	"accept":       {},
	"content-type": {},
	"user-agent":   {},
}

func (p *UpstreamProxy) openRequest(
	ctx context.Context,
	method, path, query, bearer string,
	headers http.Header,
	body io.Reader,
	allowedHeaders map[string]struct{},
) (*http.Response, error) {
	target := *p.baseURL
	target.Path = "/" + strings.TrimLeft(path, "/")
	target.RawPath = ""
	target.RawQuery = query
	request, err := http.NewRequestWithContext(ctx, method, target.String(), body)
	if err != nil {
		return nil, fmt.Errorf("build upstream request: %w", err)
	}
	for name, values := range headers {
		if _, ok := allowedHeaders[strings.ToLower(name)]; !ok || len(values) == 0 {
			continue
		}
		value := values[0]
		if strings.EqualFold(name, "User-Agent") {
			value = truncateRunes(value, 300)
		}
		request.Header.Set(name, value)
	}
	if bearer != "" {
		request.Header.Set("Authorization", "Bearer "+bearer)
	}
	// Match the Python client: avoid decompression so an upstream content-encoding and its bytes
	// remain a pair, and let the request context bound the stream rather than a total timeout.
	request.Header.Set("Accept-Encoding", "identity")
	response, err := p.client.Do(request)
	if err != nil {
		return nil, err
	}
	return response, nil
}

// relayResponse is retained for specialized upstreams such as Electric that transform their
// request before forwarding. Mounted pass-through routes use net/http/httputil.ReverseProxy.
func relayResponse(w http.ResponseWriter, response *http.Response, allowedHeaders map[string]struct{}, overrides http.Header) error {
	if response == nil {
		return errors.New("nil upstream response")
	}
	if response.Body != nil {
		defer response.Body.Close()
	}
	for name, values := range response.Header {
		if _, ok := allowedHeaders[strings.ToLower(name)]; !ok || len(values) == 0 {
			continue
		}
		w.Header().Set(name, values[0])
	}
	for name, values := range overrides {
		w.Header().Del(name)
		for _, value := range values {
			w.Header().Add(name, value)
		}
	}
	w.WriteHeader(response.StatusCode)
	if response.Body == nil {
		return nil
	}
	_, err := io.Copy(w, response.Body)
	return err
}

// GeneratedFacadeRouteAllowed keeps operator proxy calls within a versioned, read/write facade.
func GeneratedFacadeRouteAllowed(method, path string) bool {
	if method != http.MethodGet && method != http.MethodPost && method != http.MethodPatch && method != http.MethodDelete {
		return false
	}
	if !strings.HasPrefix(path, "v1/") || strings.Contains(path, `\`) {
		return false
	}
	for _, segment := range strings.Split(path, "/") {
		if segment == "" || segment == "." || segment == ".." {
			return false
		}
	}
	return true
}

func proxyClient(client *http.Client) *http.Client {
	if client == nil {
		client = &http.Client{Transport: defaultProxyTransport()}
	}
	copy := *client
	if copy.Transport == nil {
		copy.Transport = defaultProxyTransport()
	}
	copy.Timeout = 0
	copy.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return &copy
}

func defaultProxyTransport() *http.Transport {
	return &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2:     true,
		TLSHandshakeTimeout:   5 * time.Second,
		ExpectContinueTimeout: time.Second,
		IdleConnTimeout:       90 * time.Second,
		MaxIdleConns:          100,
		DisableCompression:    true,
	}
}

func truncateRunes(value string, limit int) string {
	if utf8.RuneCountInString(value) <= limit {
		return value
	}
	count := 0
	for index := range value {
		if count == limit {
			return value[:index]
		}
		count++
	}
	return value
}
