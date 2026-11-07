package gateway

import (
	"context"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
	"unicode/utf8"
)

const resultHeader = "X-Platform-Session-Proxy-Result"

type Route struct {
	Instance string `json:"instance"`
	Method   string `json:"method"`
	Path     string `json:"path"`
}
type Bearer struct {
	Token  string
	Owner  bool
	Routes []Route
}
type ProviderHTTPConfig struct {
	ID, Upstream, Origin, OwnerToken, RouteToken, CAFile string
	RequestHeaders, ResponseHeaders                      []string
}
type proxy struct {
	providers map[string]ProviderHTTPConfig
	bearers   []Bearer
}

// segments validates the escaped path once, rejecting ambiguous encodings.
func segments(raw string, pattern bool) ([]string, error) {
	if !strings.HasPrefix(raw, "/") {
		return nil, errors.New("invalid path")
	}
	if raw == "/" {
		return nil, nil
	}
	parts := strings.Split(raw[1:], "/")
	for i, part := range parts {
		if part == "" && i != len(parts)-1 {
			return nil, errors.New("invalid path")
		}
		for _, c := range part {
			if c < 32 || c >= 127 || c == '\\' || c == '?' || c == '#' {
				return nil, errors.New("invalid path")
			}
		}
		value, err := url.PathUnescape(part)
		if err != nil || !utf8.ValidString(value) || value == "." || value == ".." || strings.ContainsAny(value, "/\\%") {
			return nil, errors.New("invalid path")
		}
		for _, c := range value {
			if c < 32 || c == 127 {
				return nil, errors.New("invalid path")
			}
		}
		if pattern && strings.Contains(value, "*") && (value != "*" || part != "*") {
			return nil, errors.New("invalid wildcard")
		}
		parts[i] = value
	}
	return parts, nil
}

func escaped(parts []string) string {
	out := make([]string, len(parts))
	for i, p := range parts {
		out[i] = url.PathEscape(p)
	}
	return "/" + strings.Join(out, "/")
}

func (r Route) matches(instance, method string, path []string) bool {
	if r.Instance != instance || r.Method != method {
		return false
	}
	parts, err := segments(r.Path, true)
	if err != nil || len(parts) != len(path) {
		return false
	}
	for i, p := range parts {
		if p != path[i] && !(p == "*" && path[i] != "") {
			return false
		}
	}
	return true
}

func (p *proxy) authenticate(r *http.Request) *Bearer {
	auth := r.Header.Values("Authorization")
	if len(auth) != 1 {
		return nil
	}
	scheme, token, ok := strings.Cut(auth[0], " ")
	if !ok || !strings.EqualFold(scheme, "Bearer") {
		return nil
	}
	for i := range p.bearers {
		if subtle.ConstantTimeCompare([]byte(token), []byte(p.bearers[i].Token)) == 1 {
			return &p.bearers[i]
		}
	}
	return nil
}

var (
	requestPrivate  = map[string]bool{"authorization": true, "cookie": true, "cookie2": true, "host": true, strings.ToLower(resultHeader): true}
	responsePrivate = map[string]bool{"authorization": true, "authentication-info": true, "proxy-authentication-info": true, "set-cookie": true, "set-cookie2": true, "content-length": true, strings.ToLower(resultHeader): true}
	hopHeaders      = map[string]bool{"connection": true, "keep-alive": true, "proxy-authenticate": true, "proxy-authorization": true, "te": true, "trailer": true, "transfer-encoding": true, "upgrade": true}
)

func filterHeaders(source http.Header, private map[string]bool, extra []string) http.Header {
	blocked := map[string]bool{}
	for k := range hopHeaders {
		blocked[k] = true
	}
	for k := range private {
		blocked[k] = true
	}
	for _, k := range extra {
		blocked[strings.ToLower(k)] = true
	}
	for _, value := range source.Values("Connection") {
		for _, name := range strings.Split(value, ",") {
			blocked[strings.ToLower(strings.TrimSpace(name))] = true
		}
	}
	out := http.Header{}
	for k, values := range source {
		if !blocked[strings.ToLower(k)] {
			out[k] = append([]string(nil), values...)
		}
	}
	return out
}

func proxyError(w http.ResponseWriter, code int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_, _ = io.WriteString(w, `{"error":"`+message+`"}`)
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	principal := p.authenticate(r)
	if principal == nil {
		proxyError(w, 401, "unauthorized")
		return
	}
	raw := r.URL.EscapedPath()
	parts := strings.SplitN(raw, "/", 4)
	if len(parts) != 4 || parts[1] != "proxy" {
		proxyError(w, 404, "not_found")
		return
	}
	instance := parts[2]
	provider, ok := p.providers[instance]
	if !ok {
		proxyError(w, 404, "not_found")
		return
	}
	path, err := segments("/"+parts[3], false)
	if err != nil {
		proxyError(w, 400, "invalid_path")
		return
	}
	allowed := principal.Owner
	for _, route := range principal.Routes {
		allowed = allowed || route.matches(instance, r.Method, path)
	}
	if !allowed {
		proxyError(w, 403, "forbidden")
		return
	}
	origin, err := url.Parse(provider.Origin)
	if err != nil || origin.Scheme != "https" || origin.Host == "" || origin.User != nil || (origin.Path != "" && origin.Path != "/") || origin.RawQuery != "" || origin.Fragment != "" {
		proxyError(w, 502, "upstream_unavailable")
		return
	}
	target := strings.TrimSuffix(provider.Origin, "/") + "/internal/proxy/" + instance + escaped(path)
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	upstream, err := http.NewRequestWithContext(ctx, r.Method, target, r.Body)
	if err != nil {
		proxyError(w, 502, "upstream_unavailable")
		return
	}
	upstream.Header = filterHeaders(r.Header, requestPrivate, provider.RequestHeaders)
	token := provider.RouteToken
	if principal.Owner {
		token = provider.OwnerToken
	}
	upstream.Header.Set("Authorization", "Bearer "+token)
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	if provider.CAFile != "" {
		pem, err := os.ReadFile(provider.CAFile)
		pool := x509.NewCertPool()
		if err != nil || !pool.AppendCertsFromPEM(pem) {
			proxyError(w, 502, "upstream_unavailable")
			return
		}
		tlsConfig.RootCAs = pool
	}
	transport := &http.Transport{TLSClientConfig: tlsConfig, DisableKeepAlives: true, DisableCompression: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(upstream)
	if err != nil {
		proxyError(w, 502, "upstream_unavailable")
		return
	}
	defer response.Body.Close()
	markers := response.Header.Values(resultHeader)
	if len(markers) != 1 || markers[0] != "1" {
		proxyError(w, 502, "upstream_unavailable")
		return
	}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		proxyError(w, 502, "upstream_unavailable")
		return
	}
	headers := filterHeaders(response.Header, responsePrivate, provider.ResponseHeaders)
	if response.StatusCode >= 300 && response.StatusCode < 400 && len(headers.Values("Location")) > 0 {
		locations := headers.Values("Location")
		if len(locations) != 1 {
			proxyError(w, 502, "upstream_unavailable")
			return
		}
		location, err := rewriteLocation(locations[0], provider.Upstream, instance, path, r.URL.RawQuery)
		if err != nil {
			proxyError(w, 502, "upstream_unavailable")
			return
		}
		headers.Set("Location", location)
	}
	for name, values := range headers {
		for _, value := range values {
			w.Header().Add(name, value)
		}
	}
	w.WriteHeader(response.StatusCode)
	_, _ = w.Write(body)
}

func rewriteLocation(location, baseURL, instance string, path []string, query string) (string, error) {
	base, err := url.Parse(baseURL)
	if err != nil || base.Scheme == "" || base.Host == "" || base.User != nil {
		return "", errors.New("invalid upstream")
	}
	prefix, err := segments(base.EscapedPath(), false)
	if base.EscapedPath() == "" {
		prefix = nil
		err = nil
	}
	if err != nil {
		return "", err
	}
	combined := append(append([]string(nil), prefix...), path...)
	current, err := url.Parse(base.Scheme + "://" + base.Host + escaped(combined))
	if err != nil {
		return "", err
	}
	current.RawQuery = query
	loc, err := url.Parse(location)
	if err != nil || loc.User != nil {
		return "", errors.New("invalid redirect")
	}
	target := current.ResolveReference(loc)
	if !strings.EqualFold(target.Scheme, base.Scheme) || !strings.EqualFold(target.Host, base.Host) {
		return "", errors.New("redirect outside origin")
	}
	output, err := segments(target.EscapedPath(), false)
	if err != nil || len(output) < len(prefix) {
		return "", errors.New("invalid redirect")
	}
	for i, v := range prefix {
		if output[i] != v {
			return "", errors.New("redirect outside prefix")
		}
	}
	relative := output[len(prefix):]
	result := &url.URL{Path: "/proxy/" + instance + "/" + strings.Join(relative, "/"), RawPath: "/proxy/" + instance + escaped(relative), RawQuery: target.RawQuery, Fragment: target.Fragment}
	return result.String(), nil
}
