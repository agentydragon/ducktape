package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

var (
	bearerPattern = regexp.MustCompile(`(?i)^Bearer +([A-Za-z0-9._~+/=-]+)$`)
	hopHeaders    = map[string]struct{}{
		"connection": {}, "keep-alive": {}, "proxy-authenticate": {}, "proxy-authorization": {},
		"te": {}, "trailer": {}, "transfer-encoding": {}, "upgrade": {},
	}
	credentialHeaders = map[string]struct{}{
		"authorization": {}, "x-api-key": {}, "x-litellm-api-key": {},
	}
	untrustedMetadataHeaders = map[string]struct{}{
		"x-litellm-agent-id": {}, "x-litellm-customer-id": {}, "x-litellm-spend-logs-metadata": {},
	}
	identityHeaderPrefixes = []string{"x-agentplane-", "x-sandbox-", "x-pod-", "x-agent-", "x-thread-"}
	proxyMethods           = []string{http.MethodGet, http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete, http.MethodOptions, http.MethodHead}
)

type ingressHandler struct {
	resolver *auth.Resolver
	backend  *http.Client
	baseURL  *url.URL
	key      string
	models   map[string]modelConfig
	logBody  bool
	logger   *slog.Logger
	pool     chan struct{}
}

func newIngressHandler(c config, resolver *auth.Resolver, logger *slog.Logger) (*ingressHandler, error) {
	baseURL, err := url.Parse(c.LiteLLMURL)
	if err != nil {
		return nil, errors.New("invalid LiteLLM URL")
	}
	transport, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return nil, errors.New("default HTTP transport is not configurable")
	}
	transport = transport.Clone()
	transport.DisableCompression = true
	transport.TLSHandshakeTimeout = 5 * time.Second
	transport.MaxIdleConns = 100
	transport.MaxIdleConnsPerHost = 20
	transport.MaxConnsPerHost = 100
	dialer := &net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		conn, err := dialer.DialContext(ctx, network, address)
		if err != nil {
			return nil, err
		}
		return writeTimeoutConn{Conn: conn, timeout: 60 * time.Second}, nil
	}
	backend := &http.Client{
		Transport: transport,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	models := make(map[string]modelConfig, len(c.Models))
	for _, item := range c.Models {
		models[item.Model] = item
	}
	return &ingressHandler{
		resolver: resolver,
		backend:  backend,
		baseURL:  baseURL,
		key:      c.LiteLLMKey,
		models:   models,
		logBody:  c.LogLLMRequests,
		logger:   logger,
		pool:     make(chan struct{}, 100),
	}, nil
}

// writeTimeoutConn bounds a blocked outbound request write without imposing a
// timeout on the response stream read from the same full-duplex connection.
type writeTimeoutConn struct {
	net.Conn
	timeout time.Duration
}

func (c writeTimeoutConn) Write(data []byte) (int, error) {
	if err := c.Conn.SetWriteDeadline(time.Now().Add(c.timeout)); err != nil {
		return 0, err
	}
	return c.Conn.Write(data)
}

func (h *ingressHandler) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	if request.URL.Path == "/healthz" && request.Method == http.MethodGet {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(writer, `{"status":"ok"}`)
		return
	}
	if !allowedMethod(request.Method) {
		writer.Header().Set("Allow", strings.Join(proxyMethods, ", "))
		http.Error(writer, http.StatusText(http.StatusMethodNotAllowed), http.StatusMethodNotAllowed)
		return
	}
	principal, okay := h.authenticate(writer, request)
	if !okay {
		return
	}
	if request.URL.Path == "/agentplane/model-config" && request.Method == http.MethodGet {
		query := request.URL.Query()
		if !query.Has("model") {
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = io.WriteString(writer, `{"detail":[{"type":"missing","loc":["query","model"],"msg":"Field required","input":null}]}`)
			return
		}
		model := h.models[query.Get("model")]
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusOK)
		if model.Model == "" {
			_, _ = io.WriteString(writer, "null")
			return
		}
		_ = json.NewEncoder(writer).Encode(model)
		return
	}
	h.forward(writer, request, principal)
}

func allowedMethod(method string) bool {
	for _, allowed := range proxyMethods {
		if method == allowed {
			return true
		}
	}
	return false
}

func (h *ingressHandler) authenticate(writer http.ResponseWriter, request *http.Request) (auth.Principal, bool) {
	values := request.Header.Values("Authorization")
	if len(values) != 1 {
		writeJSONError(writer, http.StatusUnauthorized, "invalid workload bearer", "Bearer")
		return auth.Principal{}, false
	}
	match := bearerPattern.FindStringSubmatch(values[0])
	if match == nil {
		writeJSONError(writer, http.StatusUnauthorized, "invalid workload bearer", "Bearer")
		return auth.Principal{}, false
	}
	// Normalize the accepted HTTP bearer syntax before applying the shared
	// TokenReview policy.
	principal, err := h.resolver.ResolveAuthorization(request.Context(), []string{"Bearer " + match[1]})
	if errors.Is(err, auth.ErrInvalidBearer) {
		writeJSONError(writer, http.StatusUnauthorized, "invalid workload bearer", "Bearer")
		return auth.Principal{}, false
	}
	if err != nil {
		writeJSONError(writer, http.StatusInternalServerError, "workload authentication unavailable", "")
		return auth.Principal{}, false
	}
	return principal, true
}

func (h *ingressHandler) forward(writer http.ResponseWriter, incoming *http.Request, principal auth.Principal) {
	select {
	case h.pool <- struct{}{}:
		defer func() { <-h.pool }()
	case <-incoming.Context().Done():
		return
	case <-time.After(5 * time.Second):
		writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
		return
	}

	body, err := io.ReadAll(incoming.Body)
	if err != nil {
		if incoming.Context().Err() == nil {
			writeJSONError(writer, http.StatusBadRequest, "request body unavailable", "")
		}
		return
	}
	requestID := rand.Text()
	if h.logBody {
		h.logger.Info("llm request", "id", requestID, "namespace", principal.Account.Namespace,
			"pod", principal.PodName, "pod_uid", principal.PodUID, "method", incoming.Method,
			"path", incoming.URL.Path, "body", fmt.Sprintf("%q", body))
	}
	forwardURL := *h.baseURL
	basePath := strings.TrimSuffix(h.baseURL.EscapedPath(), "/")
	requestPath := strings.TrimLeft(incoming.URL.EscapedPath(), "/")
	rawPath := basePath + "/" + requestPath
	path, err := url.PathUnescape(rawPath)
	if err != nil {
		writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
		return
	}
	forwardURL.Path = path
	forwardURL.RawPath = ""
	if path != rawPath {
		forwardURL.RawPath = rawPath
	}
	forwardURL.RawQuery = incoming.URL.RawQuery
	forwardURL.ForceQuery = incoming.URL.ForceQuery
	outgoing, err := http.NewRequestWithContext(incoming.Context(), incoming.Method, forwardURL.String(), bytes.NewReader(body))
	if err != nil {
		writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
		return
	}
	outgoing.Header = forwardedRequestHeaders(incoming.Header, principal, h.key)
	response, err := h.backend.Do(outgoing)
	if err != nil {
		if incoming.Context().Err() == nil {
			writeJSONError(writer, http.StatusBadGateway, "LiteLLM backend unavailable", "")
		}
		return
	}
	defer response.Body.Close()
	if h.logBody {
		h.logger.Info("llm response start", "id", requestID, "status", response.StatusCode,
			"content_type", response.Header.Get("Content-Type"))
	}
	copyResponseHeaders(writer.Header(), response.Header)
	writer.WriteHeader(response.StatusCode)
	if err := http.NewResponseController(writer).Flush(); err != nil && !errors.Is(err, http.ErrNotSupported) {
		return
	}
	complete := false
	defer func() {
		if h.logBody {
			h.logger.Info("llm response end", "id", requestID, "complete", complete)
		}
	}()
	buffer := make([]byte, 32*1024)
	chunk := 0
	for {
		count, readErr := response.Body.Read(buffer)
		if count > 0 {
			part := buffer[:count]
			if h.logBody {
				h.logger.Info("llm response chunk", "id", requestID, "status", response.StatusCode,
					"chunk", chunk, "body", fmt.Sprintf("%q", part))
			}
			chunk++
			if _, err := writer.Write(part); err != nil {
				return
			}
			if err := http.NewResponseController(writer).Flush(); err != nil && !errors.Is(err, http.ErrNotSupported) {
				return
			}
		}
		if readErr == io.EOF {
			complete = true
			return
		}
		if readErr != nil {
			panic(http.ErrAbortHandler)
		}
	}
}

func forwardedRequestHeaders(headers http.Header, principal auth.Principal, key string) http.Header {
	connectionTokens := connectionHeaderTokens(headers)
	forwarded := make(http.Header)
	for name, values := range headers {
		lower := strings.ToLower(name)
		if _, found := hopHeaders[lower]; found {
			continue
		}
		if _, found := connectionTokens[lower]; found {
			continue
		}
		if _, found := credentialHeaders[lower]; found {
			continue
		}
		if _, found := untrustedMetadataHeaders[lower]; found || lower == "host" || lower == "content-length" {
			continue
		}
		if hasIdentityHeaderPrefix(lower) {
			continue
		}
		forwarded[name] = append([]string(nil), values...)
	}
	forwarded.Set("Authorization", "Bearer "+key)
	metadata, _ := json.Marshal(map[string]string{
		"agentplane.namespace":       principal.Account.Namespace,
		"agentplane.service_account": principal.Account.Name,
		"agentplane.pod_name":        principal.PodName,
		"agentplane.pod_uid":         principal.PodUID,
	})
	forwarded.Set("x-litellm-spend-logs-metadata", string(metadata))
	return forwarded
}

func copyResponseHeaders(destination, source http.Header) {
	connectionTokens := connectionHeaderTokens(source)
	for name, values := range source {
		lower := strings.ToLower(name)
		if _, found := hopHeaders[lower]; found {
			continue
		}
		if _, found := connectionTokens[lower]; found {
			continue
		}
		if _, found := credentialHeaders[lower]; found || lower == "content-length" {
			continue
		}
		for _, value := range values {
			destination.Add(name, value)
		}
	}
}

func connectionHeaderTokens(headers http.Header) map[string]struct{} {
	tokens := make(map[string]struct{})
	for _, value := range headers.Values("Connection") {
		for _, token := range strings.Split(value, ",") {
			if token = strings.TrimSpace(token); token != "" {
				tokens[strings.ToLower(token)] = struct{}{}
			}
		}
	}
	return tokens
}

func hasIdentityHeaderPrefix(name string) bool {
	for _, prefix := range identityHeaderPrefixes {
		if strings.HasPrefix(name, prefix) {
			return true
		}
	}
	return false
}

func writeJSONError(writer http.ResponseWriter, status int, detail, authenticate string) {
	writer.Header().Set("Content-Type", "application/json")
	if authenticate != "" {
		writer.Header().Set("WWW-Authenticate", authenticate)
	}
	writer.WriteHeader(status)
	_, _ = fmt.Fprintf(writer, `{"detail":%q}`, detail)
}
